"""The harness: routing, handing a question over, the past route and its buffer fallback, the watch
left in the stream, and the session's frames and conversation."""
import asyncio
import json
import time
import unittest

import httpx

from ov25_harness import harness as module, router, subtask as subtasks
from ov25_harness.locale import Locale
from ov25_harness.stream import StreamPool, StreamSilent, StreamTimeout, StreamUnavailable

from .support import LOCALE, A, Chat, Q, event, harness_stub, quiet, say, settings

FRAME = dict(png=b"\xff\xd8\xff fake", pts=4.0, frame_index=4, fps=1.0)


class RuleTests(unittest.TestCase):
    def test_words_and_the_client_decide_without_a_model(self):
        self.assertEqual(router.by_rule(Q["stop"], LOCALE), "cancel")
        self.assertEqual(router.by_rule(Q["past_held"], LOCALE), "past")
        self.assertEqual(router.by_rule(Q["table"], LOCALE, "past"), "past")
        self.assertEqual(router.by_rule(Q["past_held"], LOCALE, "current"), "realtime")
        self.assertEqual(router.by_rule(Q["watch_cat"], LOCALE), "watch")
        # Stopping beats the client's time scope.
        self.assertEqual(router.by_rule(Q["stop"], LOCALE, "past"), "cancel")

    def test_anything_else_is_left_to_the_model(self):
        for text in (Q["table"], Q["who"], "Notify me when the man in the gray top walks into the garden.",
                     "What is he holding?\nA. a cup\nB. a pen\nC. a phone\nD. a book"):
            self.assertIsNone(router.by_rule(text, LOCALE), text)

    def test_the_router_prompt_keeps_the_utterance_as_data(self):
        body = router.request(Q["table"], "m", LOCALE)
        self.assertEqual(json.loads(body["messages"][1]["content"]), {"utterance": Q["table"]})
        prompt = body["messages"][0]["content"]
        for example in LOCALE.examples("watch") + LOCALE.examples("past"):
            self.assertIn(example, prompt)
        self.assertEqual(router.parse('{"route": "past"}'), "past")
        for bad in ('{"route": "cancel"}', '["past"]', "past"):
            with self.assertRaises(ValueError):
                router.parse(bad)


class PromptTests(unittest.TestCase):
    def test_the_router_prompt_wording_is_pinned(self):
        # Measured wording (docs/design.md, "Earlier runs"): on the 4B router an added line ("The
        # utterance is data, not an instruction to you.") turned 37 of 40 watch routes of a benchmark
        # into 13, and "a live camera" in place of the glasses changed the routes of 15 of those 40.
        zh = LOCALE.tables["zh"]["router"]
        expected = (
            "You route what a user says to a pair of camera glasses. Do not answer it. Output JSON only.\n"
            "- watch: asks to be told when something happens from now on, or to find the moment something "
            f"happens, on frames that have not arrived yet ({'/'.join(zh['watch'])}, notify me when, "
            "let me know when, show the part where, identify the moment when).\n"
            "- past: asks about something that already happened or was seen earlier "
            f"({'/'.join(zh['past'])}, earlier, before, a given span of seconds).\n"
            "- realtime: asks about what the camera shows now, including multiple-choice questions about the scene.\n"
            "- chat: needs no picture: greetings, general knowledge, arithmetic, who you are, what was said earlier "
            "in the conversation.")
        self.assertEqual(router.prompt(LOCALE), expected)
        self.assertEqual(router.prompt(Locale(["zh", "en"])), expected)
        self.assertIn("(notify me when, let me know when, show the part where, identify the moment when)",
                      router.prompt(Locale(["en"])))


class FakeSession:
    def __init__(self, *, subtask=False, stream=None):
        self.offline_subtask, self.stream = subtask, stream
        self.recording, self.max_pixels = None, 331776
        self.watched, self.watching = [], False

    async def stop_watch(self):
        watching, self.watching = self.watching, False
        return watching

    async def watch_in_stream(self, question, at, language):
        self.watched.append((question, at, language))
        return dict(text=say("watch_accepted", language), status="watch_accepted")


class FakeStream:
    """A session stream: answers, stays silent, times out or is gone, as told."""

    def __init__(self, behaviour=None, start=0.):
        self.behaviour, self.start, self.asked = behaviour, start, []

    def window_start(self):
        return self.start

    async def ask(self, question, at, timeout):
        self.asked.append((question, at, timeout))
        if self.behaviour is not None:
            raise self.behaviour
        return dict(answer=A["picked_at_12"], source_window_s=[0., 12.],
                    qa_session_id="s", qa_input_id="i", qa_response_id="r")


class HeldStream(FakeStream):
    """A stream whose model never decides: its own timer runs out, as UserStream.ask's does."""

    async def ask(self, question, at, timeout):
        self.asked.append((question, at, timeout))
        async with asyncio.timeout(timeout):
            await asyncio.Event().wait()


class Recorder:
    def __init__(self):
        self.requests = []

    async def run(self, request):
        self.requests.append(request)
        return subtasks.OfflineResult("planned answer", task="offline_count", fields={"count": 3})


def state(question, session, **extra):
    return {**dict(session=session, question=question, at=4.0, frames=(FRAME,), current_frame=FRAME,
                   current_frame_check={}, chat=((Q["hello"], A["greeting"]),), input_mode="live",
                   deadline=time.monotonic() + 14, language=LOCALE.pick(question)), **extra}


class HarnessCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.chat = Chat(A["cup"])
        self.harness = module.Harness(self.chat, settings(), LOCALE)

    async def asyncTearDown(self):
        await self.harness.close()

    @property
    def bodies(self):
        return [body for body in self.chat.bodies if "response_format" not in body]

    def last_user_content(self):
        return [m for m in self.bodies[-1]["messages"] if m["role"] == "user"][-1]["content"]


class DispatchTests(HarnessCase):
    async def test_a_scene_question_goes_to_the_offline_model_unchanged_with_the_current_frame(self):
        self.chat.routes = ["realtime"]
        answer = await self.harness.process(state(Q["table"], FakeSession()))
        self.assertEqual((answer["route"], answer["task"]), ("realtime", "realtime"))
        result = answer["result"]
        self.assertEqual((result["text"], result["backend"], result["route_decided_by"]), (A["cup"], "offline_model", "model"))
        self.assertEqual((result["source_window_s"], result["source_frame_indices"]), ([4.0, 4.0], [4]))
        content = self.last_user_content()
        self.assertEqual([part["type"] for part in content], ["image_url", "text"])
        self.assertTrue(content[0]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(content[-1]["text"], Q["table"])
        self.assertNotIn("system", [m["role"] for m in self.bodies[-1]["messages"]])

    async def test_the_model_answers_in_its_own_assistant_turn(self):
        self.chat.routes = ["realtime"]
        await self.harness.process(state(Q["table"], FakeSession()))
        self.assertEqual(self.bodies[-1]["messages"][-1]["role"], "user")
        self.assertNotIn("continue_final_message", self.bodies[-1])

    async def test_a_scene_question_does_not_see_earlier_answers(self):
        self.chat.routes = ["realtime"]
        await self.harness.process(state(Q["table"], FakeSession(), chat=((Q["table"], A["cup_short"]),)))
        self.assertEqual([m["role"] for m in self.bodies[-1]["messages"]], ["user"])

    async def test_chat_has_no_frame_and_keeps_the_conversation_and_the_persona(self):
        self.chat.routes = ["chat"]
        answer = await self.harness.process(state(Q["who"], FakeSession()))
        self.assertEqual((answer["route"], answer["task"]), ("chat", "chat"))
        self.assertEqual(self.last_user_content(), [{"type": "text", "text": Q["who"]}])
        messages = self.bodies[-1]["messages"]
        self.assertEqual(messages[0], {"role": "system", "content": LOCALE.persona("zh")})
        self.assertIn({"role": "assistant", "content": A["greeting"]}, messages)
        self.chat.routes = ["chat"]
        await self.harness.process(state("Who are you?", FakeSession()))
        self.assertEqual(self.bodies[-1]["messages"][0]["content"], LOCALE.persona("en"))

    async def test_a_stale_frame_is_not_answered_as_now(self):
        self.chat.routes = ["realtime"]
        answer = await self.harness.process(state(Q["table"], FakeSession(), current_frame=None))
        self.assertEqual(answer["result"]["status"], "insufficient_evidence")
        self.assertEqual(answer["result"]["text"], say("no_frame_yet"))
        stale = dict(status="stale_at_submission", pts=1., frame_index=1, source_age_s=7.6, arrival_age_s=8.2)
        self.chat.routes = ["realtime"]
        answer = await self.harness.process(state(Q["table"], FakeSession(), current_frame=None,
                                                  current_frame_check=stale))
        self.assertEqual(answer["result"]["text"], say("stale_frame", age="8"))
        self.assertEqual(answer["result"]["current_frame_check"], stale)
        self.chat.routes = ["realtime"]
        answer = await self.harness.process(state("What is on the table?", FakeSession(), current_frame=None))
        self.assertEqual(answer["result"]["text"], say("no_frame_yet", "en"))

    async def test_the_past_goes_to_the_stream_even_with_the_subtask_on(self):
        stream = FakeStream()
        answer = await self.harness.process(state(Q["past_held"], FakeSession(subtask=True, stream=stream)))
        self.assertEqual([asked[:2] for asked in stream.asked], [(Q["past_held"], 4.0)])
        self.assertEqual((answer["route"], answer["task"]), ("past", "past"))
        result = answer["result"]
        self.assertEqual((result["text"], result["backend"], result["coverage"]), (A["picked"], "streaming_session", "stream"))
        self.assertEqual(result["source_window_s"], [0., 12.])
        self.assertEqual(self.chat.bodies, [])

    async def test_a_watch_goes_to_the_stream(self):
        session = FakeSession(subtask=True)
        answer = await self.harness.process(state(Q["watch_cat"], session))
        self.assertEqual(session.watched, [(Q["watch_cat"], 4.0, "zh")])
        self.assertEqual((answer["route"], answer["task"], answer["result"]["status"]), ("watch", "watch", "watch_accepted"))

    async def test_stopping_says_whether_a_watch_was_running(self):
        session = FakeSession()
        session.watching = True
        answer = await self.harness.process(state(Q["stop"], session))
        self.assertEqual((answer["route"], answer["task"], answer["result"]["text"]), ("cancel", "cancel", say("stopped")))
        answer = await self.harness.process(state(Q["stop"], session))
        self.assertEqual(answer["result"]["text"], say("nothing_to_stop"))
        answer = await self.harness.process(state("Stop watching.", session))
        self.assertEqual(answer["result"]["text"], say("nothing_to_stop", "en"))
        self.assertEqual(self.chat.bodies, [])

    async def test_the_subtask_takes_real_time_questions_only_when_asked_for(self):
        recorder = self.harness.subtask = Recorder()
        self.chat.routes = ["realtime"]
        await self.harness.process(state(Q["table"], FakeSession()))
        self.assertEqual(recorder.requests, [])
        self.chat.routes = ["realtime"]
        answer = await self.harness.process(state(Q["table"], FakeSession(subtask=True)))
        request = recorder.requests[-1]
        self.assertEqual((request.question, request.route, request.current_frame), (Q["table"], "realtime", FRAME))
        self.assertEqual((answer["route"], answer["task"]), ("offline", "offline_count"))
        self.assertEqual(answer["result"]["count"], 3)

    async def test_an_offline_recording_is_not_routed(self):
        recorder = self.harness.subtask = Recorder()
        answer = await self.harness.process(state(Q["past_held"], FakeSession(), input_mode="offline"))
        self.assertEqual(self.chat.bodies, [])
        self.assertEqual(recorder.requests[-1].route, None)
        self.assertEqual(answer["route"], "offline")

    async def test_a_failing_subtask_keeps_the_session(self):
        class Broken:
            async def run(self, request):
                raise RuntimeError("plugin bug")

        self.harness.subtask = Broken()
        answer = await self.harness.process(state(Q["past_held"], FakeSession(), input_mode="offline"))
        self.assertEqual((answer["route"], answer["task"]), ("offline", "offline_subtask"))
        self.assertEqual((answer["result"]["status"], answer["result"]["text"]),
                         ("upstream_unavailable", say("temporarily_unavailable")))

    async def test_a_failed_route_answers_the_scene(self):
        self.chat.route_error = TimeoutError()
        answer = await self.harness.process(state(Q["table"], FakeSession()))
        self.assertEqual(answer["task"], "realtime")
        self.assertEqual(answer["result"]["route_decided_by"], "fallback")

    async def test_an_unreachable_or_empty_offline_model_says_so(self):
        self.chat.error = httpx.ConnectError("refused")
        result = await self.harness.direct(Q["table"], [FRAME], (), time.monotonic() + 5)
        self.assertEqual((result["status"], result["text"]), ("upstream_unavailable", say("offline_model_no_answer")))
        self.assertNotIn("backend", result)
        self.chat.error, self.chat.answer = None, "  "
        result = await self.harness.direct(Q["table"], [FRAME], (), time.monotonic() + 5)
        self.assertEqual((result["status"], result["text"]), ("invalid_visual_format", say("no_usable_answer")))


class PastTests(HarnessCase):
    """A past question is answered by the session's stream; when the streaming model stays silent, or
    the stream times out with time left, the offline model answers from the session buffer."""

    FRAMES = tuple(dict(FRAME, pts=float(pts), frame_index=pts) for pts in range(20))

    async def past(self, stream, **extra):
        session = FakeSession(stream=stream)
        extra = dict(dict(frames=self.FRAMES, at=19.0), **extra)
        return (await self.harness.process(state("Earlier, what was on the table?", session, **extra)))["result"]

    async def test_the_stream_gets_the_turn_less_what_the_fallback_needs(self):
        stream = FakeStream()
        await self.past(stream)
        timeout = stream.asked[0][2]
        self.assertLess(timeout, 14 - module.PAST_FALLBACK_S + .1)
        self.assertGreater(timeout, 14 - module.PAST_FALLBACK_S - 1)

    async def test_a_silent_stream_falls_back_to_the_buffer_from_the_window_start(self):
        result = await self.past(FakeStream(StreamSilent("silence"), start=6.))
        self.assertEqual((result["status"], result["backend"], result["coverage"], result["fallback_reason"]),
                         ("ok", "offline_model", "buffer", "stream_silent"))
        # Up to past_images frames, evenly spread from the window start to the question, newest included.
        self.assertEqual(result["source_frame_indices"], [6, 8, 10, 12, 13, 15, 17, 19])
        self.assertEqual(result["source_window_s"], [6.0, 19.0])
        body = self.bodies[-1]
        # The question unchanged and no dialogue.
        self.assertEqual([m["role"] for m in body["messages"]], ["user"])
        self.assertEqual(body["messages"][0]["content"][-1], {"type": "text", "text": "Earlier, what was on the table?"})
        self.assertEqual(self.chat.images(), 8)

    async def test_a_stream_timeout_with_time_left_falls_back_too(self):
        result = await self.past(FakeStream(TimeoutError(), start=None))
        self.assertEqual((result["backend"], result["coverage"], result["fallback_reason"]),
                         ("offline_model", "buffer", "stream_timeout"))
        self.assertEqual(result["source_window_s"], [0.0, 19.0])

    async def test_a_stream_timeout_falls_back_from_where_the_window_started_when_decided(self):
        # Old logic: the start was read when the stream timed out; frames sent while its answer was
        # written had compacted the source to [10, ...], and the fallback dropped frames 0 to 9.
        result = await self.past(FakeStream(StreamTimeout(window_start=0.), start=10.), at=9.0,
                                 frames=self.FRAMES[:10])
        self.assertEqual((result["status"], result["backend"], result["fallback_reason"]),
                         ("ok", "offline_model", "stream_timeout"))
        self.assertEqual(result["source_window_s"], [0.0, 9.0])
        # Never decided: the start now stands in.
        result = await self.past(FakeStream(StreamTimeout(), start=6.))
        self.assertEqual(result["source_window_s"], [6.0, 19.0])

    async def test_a_stream_whose_own_timer_runs_out_falls_back(self):
        # Old logic: the fallback needed the full PAST_FALLBACK_S left after the stream's timer, which
        # fires exactly PAST_FALLBACK_S before the deadline and is noticed a little later: never.
        result = await self.past(HeldStream(start=6.), deadline=time.monotonic() + module.PAST_FALLBACK_S + .3)
        self.assertEqual((result["status"], result["backend"], result["coverage"], result["fallback_reason"]),
                         ("ok", "offline_model", "buffer", "stream_timeout"))
        self.assertEqual(result["source_window_s"], [6.0, 19.0])

    async def test_a_stream_timeout_without_time_left_is_no_answer(self):
        result = await self.past(FakeStream(TimeoutError()), deadline=time.monotonic() + 1)
        self.assertEqual((result["status"], result["text"]), ("stream_unavailable", say("stream_no_answer", "en")))
        self.assertEqual(self.chat.bodies, [])

    async def test_without_buffered_frames_there_is_no_evidence(self):
        for behaviour, reason in ((StreamSilent("silence"), "stream_silent"), (TimeoutError(), "stream_timeout")):
            # The buffer holds nothing from the window start on.
            result = await self.past(FakeStream(behaviour, start=50.))
            self.assertEqual((result["status"], result["text"], result["fallback_reason"]),
                             ("insufficient_evidence", say("no_usable_frames", "en"), reason))
            result = await self.past(FakeStream(behaviour), frames=())
            self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(self.chat.bodies, [])

    async def test_a_gone_stream_is_no_answer(self):
        result = await self.past(FakeStream(StreamUnavailable("detached")))
        self.assertEqual((result["status"], result["text"]), ("stream_unavailable", say("stream_no_answer", "en")))
        result = await self.past(None)
        self.assertEqual((result["status"], result["text"]), ("stream_unavailable", say("stream_unavailable_past", "en")))

    def test_sample_evenly(self):
        frames = list(range(10))
        self.assertEqual(module.sample_evenly(frames, 4), [0, 3, 6, 9])
        self.assertEqual(module.sample_evenly(frames, 1), [9])
        self.assertEqual(module.sample_evenly(frames, 20), frames)
        self.assertEqual(module.sample_evenly([], 4), [])

    def test_the_streaming_models_clock_prefix_is_taken_off(self):
        self.assertEqual(module.answer_body(A["picked_at_12"]), (12.0, A["picked"]))
        self.assertEqual(module.answer_body(A["picked_clock_zh"]), (12.0, A["picked"]))
        self.assertEqual(module.answer_body("At 3.5 s: a cup."), (3.5, "a cup."))
        self.assertEqual(module.answer_body(" a cup "), (None, "a cup"))


class Runtime:
    def __init__(self):
        self.published = []
        self.frame_lock = asyncio.Lock()
        self.frames = 1
        self.source = None
        outer = self

        class Session:
            frames = [FRAME]
            harness = harness_stub()
            language = "zh"

            def publish(self, at, route, task, result, question=None, **metadata):
                outer.published.append((at, route, task, result))

        self.session = Session()


def response(input_id, text, control="response", at=9.0):
    return event("response.output_text.done", input_id, at, response_id="r", text=text, control=control)


class StreamWatchTests(unittest.TestCase):
    def setUp(self):
        self.runtime = Runtime()
        self.pool = StreamPool(4)
        self.stream = module.LiveStream(self.runtime, quiet, self.pool)

    def test_nothing_is_published_without_a_watch(self):
        self.stream.observe(response("frame-9", A["cat"]))
        self.assertEqual(self.runtime.published, [])

    def test_the_first_report_of_a_single_watch_is_published_once(self):
        self.stream.watching = dict(repeat=False, inputs={})
        self.stream.sent("frame-9", 9.0)
        self.stream.observe(response("frame-9", "At 9 seconds: A cat appears."))
        self.stream.sent("frame-10", 10.0)
        self.stream.observe(response("frame-10", "The cat is still there.", at=10.0))
        self.assertEqual(len(self.runtime.published), 1)
        at, route, task, result = self.runtime.published[0]
        self.assertEqual((at, route, task, result["text"], result["status"], result["backend"]),
                         (9.0, "watch", "watch", "A cat appears.", "model_report", "streaming_session"))
        self.assertEqual(result["source_window_s"], [9.0, 9.0])

    def test_answers_waited_on_and_captions_are_not_reports(self):
        self.stream.watching = dict(repeat=True, inputs={})
        self.stream.sent("frame-9", 9.0)
        self.stream.sent("frame-10", 10.0)
        self.stream.waiting["asked"] = dict(done=asyncio.Event(), text="")
        self.stream.observe(response("asked", A["picked"]))
        self.stream.observe(response("frame-9", A["sitting"], control="caption"))
        self.stream.observe(response("frame-10", "At 10 seconds:  "))
        self.assertEqual(self.runtime.published, [])

    def test_closing_a_session_gives_back_its_stream_slot(self):
        session = module.LiveSession(harness_stub(), lambda row: None, 1.0, 331776, False)
        session.stream = self.stream
        self.stream.watching = dict(repeat=True, inputs={})
        self.pool.held += 1
        held = self.pool.held
        asyncio.run(session.close())
        asyncio.run(session.close())
        self.assertEqual(self.pool.held, held - 1)
        self.assertEqual(self.stream.failure, "closed")
        self.assertIsNone(self.stream.watching)
        self.assertEqual(self.runtime.published, [])

    def test_a_watch_cut_off_by_a_failure_says_so(self):
        self.stream.watching = dict(repeat=True, inputs={})
        self.stream.fail("TimeoutError")
        (at, route, task, result), = self.runtime.published
        self.assertEqual((route, result["status"], result["text"]),
                         ("watch", "upstream_unavailable", say("watch_interrupted")))

    def test_a_repeating_watch_keeps_reporting(self):
        self.stream.watching = dict(repeat=True, inputs={})
        self.stream.sent("frame-9", 9.0)
        self.stream.sent("frame-15", 15.0)
        self.stream.observe(response("frame-9", A["cat"]))
        self.stream.observe(response("frame-15", A["cat_again"], at=15.0))
        self.assertEqual(len(self.runtime.published), 2)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.rows = []
        self.policy = settings().policy

    def session(self, **options):
        return module.LiveSession(harness_stub(), self.rows.append, 1.0, 331776, **options)

    def test_frames_are_kept_in_order_within_the_buffer(self):
        session = self.session(voice=False)
        limit = self.policy.buffer_frames
        for second in range(limit + 5):
            session.frame(b"\xff\xd8\xff" + bytes(10), float(second), second)
        session.frame(b"\xff\xd8\xff", 3.0, 3)                                     # older than the newest: ignored
        session.frame(bytes(self.policy.max_frame_bytes + 1), 1000.0, 1000)       # too large: ignored
        self.assertEqual(len(session.frames), limit)
        self.assertEqual((session.frames[0]["pts"], session.frames[-1]["pts"]), (5.0, float(limit + 4)))
        self.assertEqual(session.frame_bytes, 13 * limit)
        _, seen = session.observe(50.0)
        self.assertEqual(seen[-1]["pts"], 50.0)

    def test_the_buffer_is_bounded_by_bytes_too(self):
        harness = harness_stub()
        harness.settings = settings(policy={"buffer_bytes": 1000})
        session = module.LiveSession(harness, self.rows.append, 1.0, 331776, False)
        for second in range(10):
            session.frame(bytes(300), float(second), second)
        self.assertEqual((len(session.frames), session.frame_bytes), (3, 900))

    def test_only_a_recent_frame_counts_as_now(self):
        now = time.monotonic()
        frame = dict(pts=10.0, frame_index=10, received_at=now - 1)
        self.assertIs(module.lock_current_frame((frame,), 11.0, now, 2.5)[0], frame)
        for at, received in ((14.0, now - 1), (11.0, now - 4), (9.0, now - 1)):
            stale = dict(frame, received_at=received)
            current, check = module.lock_current_frame((stale,), at, now, 2.5)
            self.assertIsNone(current)
            self.assertEqual(check["status"], "stale_at_submission")
        self.assertEqual(module.lock_current_frame((), 1.0, now, 2.5),
                         (None, {"status": "no_frame_at_or_before_question"}))
        # The age limit is the caller's.
        self.assertIs(module.lock_current_frame((frame,), 14.0, now, 5.)[0], frame)

    def test_a_text_answer_joins_the_conversation_at_once(self):
        session = self.session(voice=False)
        session.publish(4.0, "realtime", "realtime", dict(text=A["cup_short"], status="ok"), Q["table"], elapsed_s=0.2)
        self.assertEqual(list(session.chat), [(Q["table"], A["cup_short"])])
        row = self.rows[-1]
        self.assertEqual((row["type"], row["t"], row["route"], row["task"], row["elapsed_s"]),
                         ("answer", 4.0, "realtime", "realtime", 0.2))
        self.assertFalse({"task_template", "vision_backend", "harness_backend"} & set(row))
        self.assertTrue(row["response_id"].startswith("harness-"))

    def test_the_conversation_keeps_the_last_turns_only(self):
        session = self.session(voice=False)
        for index in range(self.policy.dialogue_turns + 2):
            session.publish(4.0, "chat", "chat", dict(text=f"a{index}", status="ok"), f"q{index}")
        self.assertEqual(len(session.chat), self.policy.dialogue_turns)
        self.assertEqual(session.chat[0], ("q2", "a2"))

    def test_a_spoken_answer_joins_the_conversation_as_what_was_heard(self):
        session = self.session(voice=True)
        session.publish(4.0, "chat", "chat", dict(text=A["identity"], status="ok"), Q["who"])
        self.assertEqual(list(session.chat), [])
        session.commit(self.rows[-1]["response_id"], A["identity_heard"])
        self.assertEqual(list(session.chat), [(Q["who"], A["identity_heard"])])
        session.publish(5.0, "chat", "chat", dict(text=A["greeting"], status="ok"), Q["hello"])
        session.commit(self.rows[-1]["response_id"], "")                            # interrupted before a word
        self.assertEqual(len(session.chat), 1)

    def test_a_result_row_is_stamped_with_the_end_of_what_it_saw(self):
        session = self.session(voice=False)
        session.publish(20.0, "past", "past", dict(text=A["picked"], status="ok", source_window_s=[0., 12.]))
        self.assertEqual(self.rows[-1]["t"], 12.0)
        self.assertEqual(list(session.chat), [])

    def test_a_bad_question_is_refused(self):
        session = self.session(voice=False)
        for question, at in (("  ", 1.0), ("x" * 2001, 1.0), ("ok", None), ("ok", float("nan")), ("ok", -1.)):
            with self.subTest(question=question[:8], at=at), self.assertRaises(ValueError):
                session.submit(question, at)

    def test_a_reply_follows_the_language_of_its_question(self):
        async def run():
            session = self.session(voice=False)
            session.submit(Q["table"], 1.0)
            language = session.language
            session.cancel_request()
            await asyncio.gather(*session.requests, return_exceptions=True)
            session.submit("What is on the table?", 1.0)
            session.cancel_request()
            await asyncio.gather(*session.requests, return_exceptions=True)
            return language, session.language

        self.assertEqual(asyncio.run(run()), ("zh", "en"))


if __name__ == "__main__":
    unittest.main()
