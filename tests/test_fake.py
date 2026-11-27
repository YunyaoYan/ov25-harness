import asyncio
import json
import unittest

from ov25_harness.fake import FakeChat, FakeStreaming, route

JPEG = b"\xff\xd8\xff\xe0fake"


def frame(t, index=0):
    return [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": JPEG}, "timestamp_sec": t,
                                          "frame_index": index, "fps": 1}]}]


def text(question, t):
    return [{"role": "user", "content": [{"type": "text", "text": question, "timestamp_sec": t}]}]


class Recorder:
    """Reads a session's events in the background, as the harness's reader does."""

    def __init__(self, session):
        self.session, self.events = session, []
        self.task = asyncio.create_task(self._read())

    async def _read(self):
        async for event in self.session.events():
            self.events.append(event)

    async def until(self, predicate):
        async with asyncio.timeout(2):
            while not predicate(self.events):
                await asyncio.sleep(0.001)

    async def done(self, input_id):
        await self.until(lambda events: any(e.type == "input.done" and e.input_id == input_id for e in events))
        return next(e for e in self.events if e.type == "input.done" and e.input_id == input_id)

    async def finished(self, response_id):
        await self.until(lambda events: any(e.type == "response.done" and e.response_id == response_id
                                            for e in events))

    def of(self, input_id):
        return [e for e in self.events if e.input_id == input_id]


class FakeStreamingTest(unittest.IsolatedAsyncioTestCase):
    async def open(self, streaming=None, **options):
        session = (streaming or FakeStreaming()).session(**options)
        await session.__aenter__()
        return session, Recorder(session)

    async def test_frames_are_silent(self):
        session, recorder = await self.open()
        ids = [await session.send_messages(messages=frame(t, t)) for t in range(3)]
        await session.close()
        await recorder.task
        self.assertEqual([e.type for e in recorder.events], ["input.done"] * 3 + ["session.closed"])
        for input_id, event in zip(ids, recorder.events, strict=False):
            self.assertEqual((event.input_id, event.control, event.status, event.response_id),
                             (input_id, "silence", "completed", None))
            self.assertEqual(event.session_id, session.session_id)
        self.assertEqual(recorder.events[-1].status, "completed")
        with self.assertRaises(RuntimeError):
            await session.send_messages(messages=frame(4))

    async def test_question_events_in_engine_order(self):
        session, recorder = await self.open()
        await session.send_messages(messages=frame(1))
        await session.send_messages(messages=frame(2))
        input_id = await session.send_messages(messages=text("What is on the table?", 2.5), input_id="q1")
        self.assertEqual(input_id, "q1")
        done = await recorder.done("q1")
        await recorder.finished(done.response_id)
        events = recorder.of("q1")
        self.assertEqual([e.type for e in events],
                         ["response.created", "input.done", "response.output_text.done", "response.done"])
        created, decided, output, terminal = events
        self.assertEqual((created.status, created.control), ("in_progress", "response"))
        self.assertEqual((decided.control, decided.status, decided.timestamp_sec), ("response", "completed", 2.5))
        self.assertTrue(decided.response_id)
        self.assertEqual({e.response_id for e in events}, {decided.response_id})
        self.assertTrue(output.text.startswith("At 2.5 seconds: "))
        self.assertIn("2 frames", output.text)
        self.assertEqual(output.control, "response")
        self.assertEqual(terminal.status, "completed")
        self.assertIsNone(terminal.reason)
        flat = decided.to_dict()
        self.assertEqual((flat["type"], flat["control"]), ("input.done", "response"))
        self.assertIn("timing", flat)
        await session.close()

    async def test_compaction_keeps_compact_to_visual_frames(self):
        session, recorder = await self.open(max_visual_frames=4, compact_to_visual_frames=2)
        ids = [await session.send_messages(messages=frame(t, t)) for t in range(8)]
        await session.close()
        await recorder.task
        compacted = [e for e in recorder.events if e.type == "context.compacted"]
        self.assertEqual([e.input_id for e in compacted], [ids[4], ids[7]])
        for event in compacted:
            self.assertEqual(event.to_dict()["retained_turns"], 2)
            self.assertEqual(event.metadata["retained_visual_frames"], 2)
        # The compaction is reported before its input's input.done.
        index = recorder.events.index(compacted[0])
        self.assertEqual((recorder.events[index + 1].type, recorder.events[index + 1].input_id),
                         ("input.done", ids[4]))

    async def test_compaction_defaults_and_bad_options(self):
        session, recorder = await self.open(max_visual_frames=2)
        for t in range(3):
            await session.send_messages(messages=frame(t))
        await session.close()
        await recorder.task
        compacted = [e for e in recorder.events if e.type == "context.compacted"]
        self.assertEqual(len(compacted), 1)
        self.assertEqual(compacted[0].metadata, {"retained_turns": 2, "retained_visual_frames": 2})
        with self.assertRaises(RuntimeError):
            await FakeStreaming().session(compact_to_visual_frames=2).__aenter__()
        with self.assertRaises(RuntimeError):
            await FakeStreaming().session(max_visual_frames=2, compact_to_visual_frames=3).__aenter__()

    async def test_unknown_options_are_ignored(self):
        async with FakeStreaming().session(enable=[], caption_prompt="x", verification=None) as session:
            self.assertTrue(session.session_id)

    async def test_captions_every_third_frame(self):
        session, recorder = await self.open(enable=["caption"], max_responses=3)
        ids = [await session.send_messages(messages=frame(t, t)) for t in range(6)]
        await session.close()
        await recorder.task
        controls = [next(e.control for e in recorder.of(i) if e.type == "input.done") for i in ids]
        self.assertEqual(controls, ["silence", "silence", "caption", "silence", "silence", "caption"])
        captions = [e for e in recorder.events if e.type == "response.output_text.done"]
        self.assertEqual([(e.control, e.text) for e in captions],
                         [("caption", "At 0 - 2 seconds: frame 3."), ("caption", "At 3 - 5 seconds: frame 6.")])

    async def test_no_captions_without_enable(self):
        session, recorder = await self.open()
        for t in range(6):
            await session.send_messages(messages=frame(t))
        await session.close()
        await recorder.task
        self.assertFalse([e for e in recorder.events if e.type == "response.created"])

    async def watch(self, question):
        session, recorder = await self.open()
        await session.send_messages(messages=frame(0))
        asked = await session.send_messages(messages=text(question, 0))
        ids = [await session.send_messages(messages=frame(t, t)) for t in range(1, 27)]
        await session.close()
        await recorder.task
        self.assertEqual(next(e for e in recorder.of(asked) if e.type == "input.done").control, "silence")
        answers = {}
        for t, input_id in zip(range(1, 27), ids, strict=False):
            for event in recorder.of(input_id):
                if event.type == "response.output_text.done":
                    answers[t] = event.text
        return answers

    async def test_watch_answers_on_fifth_frame_then_every_tenth(self):
        answers = await self.watch("Let me know when you see a red car.")
        self.assertEqual(sorted(answers), [5, 15, 25])
        self.assertEqual(answers[5], "At 5 seconds: a red car is visible.")
        for question in ("Notify me when a dog appears", "Tell me when the door opens", "Alert me if a cat shows up"):
            self.assertEqual(sorted(await self.watch(question)), [5, 15, 25])

    async def test_watch_in_chinese(self):
        for question in ("\u6709\u4eba\u6765\u4e86\u63d0\u9192\u6211", "\u770b\u5230\u732b\u901a\u77e5\u6211"):
            self.assertEqual(sorted(await self.watch(question)), [5, 15, 25])

    async def test_cancel_running_branch(self):
        session, recorder = await self.open(FakeStreaming(delay=30))
        done = await recorder.done(await session.send_messages(messages=text("What is this?", 1)))
        self.assertTrue(await session.cancel_response(done.response_id))
        await recorder.finished(done.response_id)
        events = [e for e in recorder.events if e.response_id == done.response_id]
        self.assertEqual([e.type for e in events], ["response.created", "input.done", "response.done"])
        self.assertEqual((events[-1].status, events[-1].reason), ("cancelled", "user"))
        self.assertFalse(await session.cancel_response(done.response_id))
        self.assertFalse(await session.cancel_response("nothing"))
        await session.close()

    async def test_capacity_cancels_oldest_branch(self):
        session, recorder = await self.open(FakeStreaming(delay=30), max_responses=1)
        first = await recorder.done(await session.send_messages(messages=text("One?", 1)))
        second = await recorder.done(await session.send_messages(messages=text("Two?", 2)))
        await recorder.finished(first.response_id)
        terminal = next(e for e in recorder.events if e.type == "response.done")
        self.assertEqual((terminal.response_id, terminal.status, terminal.reason),
                         (first.response_id, "cancelled", "capacity"))
        await session.abort()
        await recorder.task
        terminal = [e for e in recorder.events if e.type == "response.done"][-1]
        self.assertEqual((terminal.response_id, terminal.status, terminal.reason),
                         (second.response_id, "cancelled", "session_aborted"))
        self.assertEqual((recorder.events[-1].type, recorder.events[-1].status), ("session.closed", "cancelled"))

    async def test_responder_overrides_answers(self):
        seen = []

        def responder(question, frames_seen, t):
            seen.append((question, frames_seen, t))
            return None if question == "skip" else f"{question} after {frames_seen} at {t:g}"

        session, recorder = await self.open(FakeStreaming(responder=responder))
        await session.send_messages(messages=frame(1))
        silent = await recorder.done(await session.send_messages(messages=text("skip", 1.5)))
        answered = await recorder.done(await session.send_messages(messages=text("Let me know when X", 2)))
        await recorder.finished(answered.response_id)
        self.assertEqual(silent.control, "silence")
        output = next(e for e in recorder.events if e.type == "response.output_text.done")
        self.assertEqual(output.text, "Let me know when X after 1 at 2")
        self.assertEqual(seen, [("skip", 1, 1.5), ("Let me know when X", 1, 2.0)])
        await session.close()

    async def test_commit_answer(self):
        session, recorder = await self.open(answer_commit="client")
        done = await recorder.done(await session.send_messages(messages=text("Hi?", 1)))
        await recorder.finished(done.response_id)
        self.assertTrue(await session.commit_answer(done.response_id, "Hi"))
        self.assertFalse(await session.commit_answer(done.response_id, "Hi"))
        await session.close()
        session, recorder = await self.open()
        done = await recorder.done(await session.send_messages(messages=text("Hi?", 1)))
        await recorder.finished(done.response_id)
        self.assertFalse(await session.commit_answer(done.response_id, "Hi"))
        await session.close()

    async def test_wait_response(self):
        session, recorder = await self.open(FakeStreaming(delay=0.01))
        done = await recorder.done(await session.send_messages(messages=text("What?", 1)))
        event = await session.wait_response(done.response_id)
        self.assertEqual(event.status, "completed")
        await session.close()

    async def test_input_checks(self):
        session, recorder = await self.open()
        system = [{"role": "system", "content": "Be brief."}, *frame(0)]
        await session.send_messages(messages=system, input_id="a")
        with self.assertRaises(RuntimeError):
            await session.send_messages(messages=frame(1), input_id="a")
        with self.assertRaises(RuntimeError):
            await session.send_messages(messages=system)
        await session.close()
        with self.assertRaises(RuntimeError):
            await FakeStreaming().session().send_messages(messages=frame(0))

    async def test_context_manager_closes(self):
        streaming = FakeStreaming()
        async with streaming.session() as session:
            recorder = Recorder(session)
            await session.send_messages(messages=frame(0))
        await recorder.task
        self.assertEqual(recorder.events[-1].type, "session.closed")
        self.assertEqual(streaming.sessions, [session])
        self.assertEqual(len(session.inputs), 1)


def routing(utterance):
    return {"model": "m", "response_format": {"type": "json_schema"},
            "messages": [{"role": "system", "content": "Route it."},
                         {"role": "user", "content": json.dumps({"utterance": utterance})}]}


class FakeChatTest(unittest.IsolatedAsyncioTestCase):
    async def test_routing(self):
        chat = FakeChat()
        cases = {
            "Notify me when a bus comes": "watch",
            "let me know when the kettle boils": "watch",
            "Tell me when he leaves": "watch",
            "Remind me when it rains": "watch",
            "\u732b\u6765\u4e86\u63d0\u9192\u6211": "watch",
            "\u6709\u4eba\u901a\u77e5\u6211": "watch",
            "What did I hold earlier?": "past",
            "What was there before?": "past",
            "What did he say a moment ago?": "past",
            "\u521a\u624d\u6211\u62ff\u7684\u662f\u4ec0\u4e48": "past",
            "Hello there": "chat",
            "Who are you?": "chat",
            "What is 3 + 4?": "chat",
            "What is on the table?": "realtime",
        }
        for utterance, expected in cases.items():
            content, reason = await chat.complete(routing(utterance), timeout=1)
            self.assertEqual((json.loads(content), reason), ({"route": expected}, "stop"), utterance)
            self.assertEqual(route(utterance), expected)

    async def test_answers(self):
        chat = FakeChat()
        image = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA=="}}
        body = {"messages": [{"role": "system", "content": "persona"},
                             {"role": "user", "content": [image, image, {"type": "text", "text": "What is it?"}]}]}
        self.assertEqual(await chat.complete(body, 1), ("[fake offline model] saw 2 image(s): What is it?", "stop"))
        body = {"messages": [{"role": "user", "content": "Hi"}]}
        self.assertEqual((await chat.complete(body, 1))[0], "[fake offline model] saw 0 image(s): Hi")
        video = {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,AA=="}}
        body = {"messages": [{"role": "user", "content": [video, {"type": "text", "text": "Summarize."}]}]}
        self.assertEqual((await chat.complete(body, 1))[0], "[fake offline model] saw a video: Summarize.")
        self.assertEqual(len(chat.requests), 3)


if __name__ == "__main__":
    unittest.main()
