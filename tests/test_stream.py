"""A live session's streaming source on the harness side: opening it within the pool, asking it a
question without forcing an answer, where its window starts, and dropping it."""
import asyncio
from collections import deque
from types import SimpleNamespace
import unittest
import uuid

from ov25_harness import stream as module
from ov25_harness.harness import LiveStream

from .support import HOLD, OPEN, A, Engine, Q, event, harness_stub, quiet


class Runtime:
    """The fields of SessionRuntime a source relies on, with a reader that shows every event to the
    harness side first, as the real one does."""

    def __init__(self, source):
        self.source = source
        self.published = []
        self.session = SimpleNamespace(
            frames=deque(), stream=None, harness=harness_stub(), language="en",
            publish=lambda at, route, task, result, *rest, **fields: self.published.append((at, route, task, result)))
        self.frame_lock = asyncio.Lock()
        self.frames = 0
        self._second_of_input = {}
        self.reader = asyncio.create_task(self.read())

    async def read(self):
        async for item in self.source.events():
            if self.session.stream is not None:
                self.session.stream.observe(item)

    def frame(self, pts):
        """A frame sent to the source: the input_id it went as."""
        input_id = uuid.uuid4().hex
        self.session.stream.sent(input_id, float(pts))
        self.session.frames.append(dict(pts=float(pts)))
        self.frames += 1
        return input_id


class Client:
    def __init__(self, engine):
        self.engine, self.options = engine, None

    def session(self, **options):
        self.options = options
        return self.engine.session(**options)


class StreamCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.pool = module.StreamPool(1)
        # Registered first, so it runs after every other cleanup.
        self.addCleanup(lambda: self.assertEqual(self.pool.held, 0))

    async def opened(self, engine=None, kind=module.UserStream):
        engine = engine or Engine(A["drank"])
        records = []

        def audit(kind, **data):
            records.append(data)

        opened = await module.open_user_stream(self.pool, Client(engine), audit=audit, max_responses=1)
        runtime = Runtime(opened)
        runtime.session.stream = kind(runtime, audit, self.pool)
        self.addAsyncCleanup(self.close, runtime)
        return runtime, opened, records

    async def close(self, runtime):
        runtime.reader.cancel()
        await asyncio.gather(runtime.reader, return_exceptions=True)
        if runtime.session.stream is not None:
            runtime.session.stream.release()


class UserStreamTests(StreamCase):
    async def test_question_is_asked_after_the_frames_already_sent_without_forcing(self):
        runtime, opened, _ = await self.opened()
        self.assertIs(opened, runtime.source)
        for pts in range(4):
            runtime.frame(pts)
        result = await runtime.session.stream.ask(Q["past_drink"], 2.5, 5)
        part = runtime.source.sent[-1][-1]["content"][0]
        self.assertNotIn("force_response", part)
        self.assertEqual((part["type"], part["text"]), ("text", Q["past_drink"]))
        # The question cannot precede a frame the source has already seen.
        self.assertEqual(part["timestamp_sec"], 3.0)
        self.assertEqual(result["answer"], A["drank"])
        # Before any compaction the window starts at the first frame.
        self.assertEqual(result["source_window_s"], [0.0, 3.0])
        self.assertEqual(result["control"], "response")

    async def test_a_second_session_stays_on_the_harness(self):
        await self.opened()
        records = []
        refused = await module.open_user_stream(self.pool, Client(Engine()), audit=lambda kind, **data: records.append(data))
        self.assertIsNone(refused)
        self.assertEqual(records[-1]["reason"], "limit")

    async def test_options_the_public_engine_does_not_have_are_not_sent(self):
        client = Client(Engine())
        await module.open_user_stream(self.pool, client, audit=quiet, max_responses=1)
        self.pool.held -= 1
        self.assertFalse({"max_source_tokens", "control_bias", "caption_bias", "force_response"} & set(client.options))

    async def test_caption_source_keeps_several_responses(self):
        for enable, expected in (([], 1), (["caption"], module.CAPTION_RESPONSES)):
            client = Client(Engine())
            await module.open_user_stream(self.pool, client, audit=quiet, enable=enable, max_responses=1)
            self.pool.held -= 1
            self.assertEqual((client.options["enable"], client.options["max_responses"]), (enable, expected))

    async def test_engine_refusal_keeps_the_slot_count(self):
        engine, records = Engine(), []
        engine.fail_open = True
        session = await module.open_user_stream(self.pool, Client(engine), audit=lambda kind, **data: records.append(data))
        self.assertIsNone(session)
        self.assertEqual(records[-1]["status"], "unavailable")
        self.assertEqual(self.pool.held, 0)

    async def test_drop_detaches_the_source_and_releases_a_waiting_question(self):
        runtime, _, records = await self.opened(Engine(None))
        source = runtime.source
        runtime.frame(0)
        gate = asyncio.Event()
        send = source.send_messages

        async def stall(**kwargs):
            await gate.wait()
            return await send(**kwargs)

        source.send_messages = stall
        asking = asyncio.create_task(runtime.session.stream.ask(Q["past_short"], 1, 5))
        await asyncio.sleep(.05)
        runtime._second_of_input["frame"] = 1.
        self.assertTrue(module.drop_user_stream(runtime, TimeoutError()))
        self.assertIsNone(runtime.source)
        self.assertIsNone(runtime.session.stream)
        self.assertEqual(runtime._second_of_input, {})
        gate.set()
        with self.assertRaises(module.StreamUnavailable):
            await asyncio.wait_for(asking, 1)
        await asyncio.sleep(0)
        self.assertTrue(source.aborted)
        self.assertEqual(records[-1]["reason"], "TimeoutError")
        # A session that has no source any more is not touched.
        self.assertFalse(module.drop_user_stream(runtime, "backlog"))

    async def test_a_model_that_stays_silent_is_not_an_answer(self):
        runtime, _, _ = await self.opened(Engine(None))
        runtime.frame(0)
        with self.assertRaises(module.StreamSilent):
            await runtime.session.stream.ask(Q["past_short"], 1, 2)

    async def test_a_failed_or_empty_answer_is_not_returned(self):
        for reply, status in ((A["drank"], "failed"), ("  ", "completed")):
            engine = Engine(reply)
            engine.status = status
            runtime, _, _ = await self.opened(engine)
            runtime.frame(0)
            with self.subTest(status=status, reply=reply), self.assertRaises(module.StreamUnavailable):
                await runtime.session.stream.ask(Q["past_short"], 1, 2)
            await self.close(runtime)
            runtime.session.stream = None

    async def test_no_frames_yet(self):
        runtime, _, _ = await self.opened()
        with self.assertRaises(module.StreamUnavailable):
            await runtime.session.stream.ask(Q["past_short"], 0, 1)


class WindowTests(StreamCase):
    async def test_the_window_starts_at_the_oldest_frame_the_source_still_holds(self):
        runtime, _, _ = await self.opened()
        stream = runtime.session.stream
        self.assertIsNone(stream.window_start())
        ids = [runtime.frame(pts) for pts in range(5)]
        self.assertEqual(stream.window_start(), 0.0)
        runtime.frame(5)
        # The input of frame 4 compacted the source to its newest 2 frames: 3 and 4. Frame 5,
        # sent after it, does not count against the 2.
        stream.observe(event("context.compacted", ids[4], retained_turns=2, retained_visual_frames=2))
        self.assertEqual(stream.window_start(), 3.0)
        result = await stream.ask(Q["past_drink"], 5, 5)
        self.assertEqual(result["source_window_s"], [3.0, 5.0])
        # A later compaction moves the start on; an older one cannot move it back.
        stream.observe(event("context.compacted", None, retained_turns=1, retained_visual_frames=1))
        self.assertEqual(stream.window_start(), 5.0)
        stream.observe(event("context.compacted", ids[2], retained_turns=1, retained_visual_frames=1))
        self.assertEqual(stream.window_start(), 5.0)

    async def test_a_compaction_by_a_frame_still_being_decided_counts_for_the_question(self):
        # Old logic: the start was read when the question was sent. The engine takes inputs in order,
        # so the compaction caused by frame 9 (sent, not yet decided) arrived after that and the
        # answer claimed [0, 9], frames the source had already dropped.
        runtime, _, _ = await self.opened(Engine(HOLD))
        stream, source = runtime.session.stream, runtime.source
        ids = [runtime.frame(pts) for pts in range(10)]
        asking = asyncio.create_task(stream.ask(Q["past_drink"], 9, 5))
        await asyncio.sleep(.05)
        self.assertEqual(stream.window_start(), 0.0)
        source.emit("context.compacted", ids[9], 9.0, retained_turns=1, retained_visual_frames=4)
        source.emit("input.done", ids[9], 9.0, control="silence")
        source.respond(source.held[-1], A["drank"], 9.0)
        result = await asyncio.wait_for(asking, 1)
        self.assertEqual(result["source_window_s"], [6.0, 9.0])

    async def test_a_timed_out_answer_says_where_the_window_started_when_it_was_decided(self):
        # Old logic: TimeoutError carried no start, so the fallback read the live start at timeout.
        # Frames sent while the answer was being written had compacted the source by then, and the
        # fallback lost buffer frames the source still held when it took the question.
        runtime, _, _ = await self.opened(Engine(OPEN))
        stream, source = runtime.session.stream, runtime.source
        for pts in range(10):
            runtime.frame(pts)
        asking = asyncio.create_task(stream.ask(Q["past_drink"], 9, .3))
        await asyncio.sleep(.05)
        later = [runtime.frame(pts) for pts in (10, 11)]
        source.emit("context.compacted", later[-1], 11.0, retained_turns=1, retained_visual_frames=2)
        await asyncio.sleep(.05)
        self.assertEqual(stream.window_start(), 10.0)
        with self.assertRaises(TimeoutError) as caught:
            await asyncio.wait_for(asking, 1)
        self.assertIsInstance(caught.exception, module.StreamTimeout)
        self.assertEqual(caught.exception.window_start, 0.0)
        # Never decided: nothing is known about where the source stood.
        runtime.frame(12)
        source.engine.reply = HOLD
        with self.assertRaises(module.StreamTimeout) as caught:
            await stream.ask(Q["past_drink"], 12, .1)
        self.assertIsNone(caught.exception.window_start)

    async def test_a_silent_answer_says_where_the_window_started(self):
        runtime, _, _ = await self.opened(Engine(HOLD))
        stream, source = runtime.session.stream, runtime.source
        ids = [runtime.frame(pts) for pts in range(10)]
        asking = asyncio.create_task(stream.ask(Q["past_drink"], 9, 5))
        await asyncio.sleep(.05)
        source.emit("context.compacted", ids[9], 9.0, retained_turns=1, retained_visual_frames=4)
        source.emit("input.done", source.held[-1], 9.0, control="silence")
        with self.assertRaises(module.StreamSilent) as caught:
            await asyncio.wait_for(asking, 1)
        self.assertEqual(caught.exception.window_start, 6.0)


class CaptionControlTests(StreamCase):
    """In a caption session the model may take a question as a caption step: that is no answer, and
    the caption it opens belongs to the session."""

    async def test_a_question_taken_as_a_caption_is_silent_at_once(self):
        # Old logic: input.done with a response_id was waited on to its response.done whatever its
        # control, so the caption's whole generation delayed StreamSilent, or the timeout cancelled it.
        runtime, _, _ = await self.opened(Engine(HOLD))
        source = runtime.source
        runtime.frame(0)
        asking = asyncio.create_task(runtime.session.stream.ask(Q["past_short"], 1, 2))
        await asyncio.sleep(.05)
        source.respond(source.held[-1], "", 1.0, control="caption", finish=False)
        started = asyncio.get_running_loop().time()
        with self.assertRaises(module.StreamSilent):
            await asyncio.wait_for(asking, 1)
        self.assertLess(asyncio.get_running_loop().time() - started, .5)
        await asyncio.sleep(.05)
        self.assertEqual(source.cancelled, [])

    async def test_a_caption_opened_by_a_given_up_question_is_not_cancelled(self):
        runtime, _, _ = await self.opened(Engine(HOLD))
        source = runtime.source
        runtime.frame(0)
        with self.assertRaises(TimeoutError):
            await runtime.session.stream.ask(Q["past_short"], 1, .1)
        source.respond(source.held[-1], "", 1.0, control="caption", finish=False)
        await asyncio.sleep(.05)
        self.assertEqual(source.cancelled, [])


class AbandonTests(StreamCase):
    """A question given up on (timed out or superseded) owns its late answer; it is cancelled and
    never published as a watch alert."""

    async def test_a_late_answer_to_a_timed_out_question_is_not_a_watch_alert(self):
        # Old logic: LiveStream.observe excluded only the input_ids still in `waiting`. ask() pops its
        # id in `finally`, so once the question timed out its late answer passed the check and was
        # published as a model_report, and a one-shot watch was spent on it.
        runtime, _, _ = await self.opened(Engine(HOLD), kind=LiveStream)
        stream = runtime.session.stream
        runtime.frame(0)
        await stream.watch("Let me know when you see a dog.", 0)
        with self.assertRaises(TimeoutError):
            await stream.ask(Q["past_short"], 1, .1)
        input_id = runtime.source.held[-1]
        response_id = runtime.source.respond(input_id, A["picked"], 1.0)
        await asyncio.sleep(.05)
        self.assertEqual(runtime.published, [])
        self.assertIsNotNone(stream.watching)
        self.assertIn(input_id, stream.abandoned)
        # Its id was not known when it was given up on, so it is cancelled when input.done names it.
        self.assertEqual(runtime.source.cancelled, [response_id])
        # A real alert still gets through.
        frame_id = runtime.frame(9)
        stream.observe(event("response.output_text.done", frame_id, 9.0, response_id="r9", text=A["cat_at_9"],
                             control="response"))
        self.assertEqual([(at, route, result["text"]) for at, route, _, result in runtime.published],
                         [(9.0, "watch", A["cat"])])

    async def test_a_replacement_watch_ignores_the_previous_watches_question_and_frames(self):
        runtime, source, _ = await self.opened(Engine(HOLD), kind=LiveStream)
        stream = runtime.session.stream
        await stream.watch(Q["watch_cat"], 0)
        old_question = source.held[-1]
        old_frame = runtime.frame(1)
        await stream.watch(Q["watch_dog"], 1)
        new_frame = runtime.frame(2)
        source.respond(old_question, "A cat was seen.", 0)
        source.respond(old_frame, "A cat was seen again.", 1)
        source.respond(new_frame, "A dog was seen.", 2)
        await asyncio.sleep(0)
        self.assertEqual([item[3]["text"] for item in runtime.published], ["A dog was seen."])

    async def test_an_open_answer_is_cancelled_when_its_question_is_given_up_on(self):
        runtime, _, _ = await self.opened(Engine(OPEN))
        stream = runtime.session.stream
        runtime.frame(0)
        with self.assertRaises(TimeoutError):
            await stream.ask(Q["past_short"], 1, .1)
        await asyncio.sleep(.05)
        self.assertEqual(len(runtime.source.cancelled), 1)
        self.assertTrue(runtime.source.cancelled[0].startswith("resp-"))

    async def test_a_superseded_question_is_given_up_on_too(self):
        runtime, _, _ = await self.opened(Engine(HOLD))
        runtime.frame(0)
        asking = asyncio.create_task(runtime.session.stream.ask(Q["past_short"], 1, 5))
        await asyncio.sleep(.05)
        asking.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asking
        self.assertIn(runtime.source.held[-1], runtime.session.stream.abandoned)
        self.assertEqual(runtime.session.stream.waiting, {})

    async def test_the_abandoned_ids_are_bounded(self):
        runtime, _, _ = await self.opened()
        stream = runtime.session.stream
        for index in range(module.ABANDONED + 10):
            stream.abandon(f"q{index}", {})
        self.assertEqual(len(stream.abandoned), module.ABANDONED)
        self.assertNotIn("q0", stream.abandoned)
        self.assertIn(f"q{module.ABANDONED + 9}", stream.abandoned)


class PoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_cancelled_open_gives_back_its_slot(self):
        # Old logic: the held count was undone only on Exception. CancelledError is a BaseException,
        # so a client that went away during the open left the count one higher for good, and after
        # `limit` of them every live session was refused.
        started = asyncio.Event()

        class Stuck:
            session_id = "stuck"

            async def __aenter__(self):
                started.set()
                await asyncio.sleep(60)

        pool = module.StreamPool(1)
        client = SimpleNamespace(session=lambda **options: Stuck())
        task = asyncio.create_task(module.open_user_stream(pool, client, audit=quiet, enable=[]))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(pool.held, 0)
        # The slot is really free: the next open gets it.
        self.assertIsNotNone(await module.open_user_stream(pool, Engine(), audit=quiet))
        self.assertEqual(pool.held, 1)

    async def test_pools_are_independent(self):
        small, large = module.StreamPool(1), module.StreamPool(2)
        engine = Engine()
        self.assertIsNotNone(await module.open_user_stream(small, engine, audit=quiet))
        self.assertIsNone(await module.open_user_stream(small, engine, audit=quiet))
        for _ in range(2):
            self.assertIsNotNone(await module.open_user_stream(large, engine, audit=quiet))
        self.assertIsNone(await module.open_user_stream(large, engine, audit=quiet))
        self.assertEqual((small.held, large.held), (1, 2))


if __name__ == "__main__":
    unittest.main()
