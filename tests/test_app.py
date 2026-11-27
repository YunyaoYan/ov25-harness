"""The HTTP and audio API, with the two models replaced: the streaming engine by a source that answers
what the real one would, the offline model by a chat backend that records what it was sent."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import time
import unittest
from unittest.mock import patch

import httpx
import numpy as np
from starlette.testclient import TestClient

from ov25_harness import __version__, app as app_module, audio as audio_module, subtask as subtasks
from ov25_harness.app import build_app
from ov25_harness.frame_health import FrameHealth

from .support import HOLD, JPEG, LOCALE, A, Chat, Engine, Q, say, settings

HEADERS = {"Content-Type": "image/jpeg"}


class Recording:
    fps, timestamps, end, path, size = 25.0, tuple(i / 25 for i in range(150)), 149 / 25, None, (640, 360)

    def __init__(self):
        self.decodes, self.forgotten = [], False

    def seconds(self, rate, size):
        self.decodes.append((rate, size))

        async def frames():
            return [dict(png=JPEG, pts=float(s), timestamp=float(s), frame_index=s * 25, fps=25.0) for s in (0, 3, 5)]
        return asyncio.ensure_future(frames())

    async def forget(self):
        self.forgotten = True


class GatewayTests(unittest.TestCase):
    voice = False
    policy = {}

    def setUp(self):
        self.chat = Chat(A["cup"])
        self.engine = Engine()
        backends = dict(transcriber=Transcriber(), speaker=Speaker()) if self.voice else {}
        self.settings = settings(streaming={"user_streams": 2}, policy={"live_idle_s": 60., **self.policy})
        self.app = build_app(self.settings, chat=self.chat, streaming=self.engine, **backends)
        self.patches = [patch.object(app_module, "MIN_FRAME_WALL_GAP_S", 0.)]
        for item in self.patches:
            item.start()
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        for item in self.patches:
            item.stop()

    @property
    def bodies(self):
        return [body for body in self.chat.bodies if "response_format" not in body]

    def runtime(self, root):
        return self.app.state.sessions[root.rsplit("/", 1)[-1]]

    def open(self, **body):
        reply = self.client.post("/v1/sessions", json={"mode": "qa", "fps": 1.0, **body})
        self.assertEqual(reply.status_code, 201, reply.text)
        return "/v1/sessions/" + reply.json()["session_id"], reply.json()

    def frame(self, root, t, data=JPEG):
        return self.client.post(f"{root}/frame", params={"t": t}, content=data, headers=HEADERS)

    def answer(self, root, text, t=None, after=0, **body):
        reply = self.client.post(f"{root}/text", json={"text": text, **({} if t is None else {"t": t}), **body})
        self.assertEqual(reply.status_code, 200, reply.text)
        return self.next_answer(root, after)

    def next_answer(self, root, after=0):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            rows = self.client.get(f"{root}/results", params={"after": after, "wait": 1}).json()["results"]
            for row in rows:
                after = max(after, row["seq"])
                if row["type"] == "answer":
                    return row
        self.fail("no answer")


class LiveTests(GatewayTests):
    def test_health_names_the_version_in_the_body_and_on_every_response(self):
        reply = self.client.get("/healthz")
        self.assertEqual(reply.headers["x-ov25-harness-version"], __version__)
        self.assertEqual(reply.json(), {
            "ok": True, "version": __version__, "api_version": 1, "sessions": 0, "closing_sessions": 0,
            "voice": False, "languages": ["en", "zh"], "streaming": "ov-streaming", "chat": "openai",
            "subtask": "direct"})
        for response in (self.client.get("/v1/sessions/nope/results"), self.client.post("/v1/sessions", json={"x": 1})):
            self.assertEqual(response.headers["x-ov25-harness-version"], __version__)
        self.open()
        self.assertEqual(self.client.get("/healthz").json()["sessions"], 1)

    def test_a_live_session_has_its_own_source_and_gives_its_slot_back(self):
        root, reply = self.open()
        self.assertEqual((reply["mode"], reply["max_visual_frames"], reply["compact_to_visual_frames"],
                          reply["frame_interval_s"], reply["api_version"]), ("qa", 150, 75, 1, 1))
        self.assertLessEqual({"frame_url", "text_url", "results_url", "events_url", "watch_url", "logs_url", "logs_token"},
                             set(reply))
        self.assertTrue(reply["frame_url"].startswith("/v1/sessions/"))
        options = self.engine.sources[0].options
        self.assertEqual((options["enable"], options["max_responses"], options["answer_commit"]), ([], 1, "client"))
        self.open()
        refused = self.client.post("/v1/sessions", json={"mode": "qa"})
        self.assertEqual((refused.status_code, refused.json()["detail"]), (503, "no streaming capacity for a live session"))
        closed = self.client.delete(root).json()
        self.assertEqual((closed["closed"], closed["frames"]), (True, 0))
        self.assertTrue(self.engine.sources[0].closed)
        self.open()
        self.assertEqual(self.client.get(f"{root}/results").status_code, 404)

    def test_a_client_may_ask_for_a_larger_source_within_the_limit(self):
        _, reply = self.open(max_visual_frames=300, compact_to_visual_frames=100)
        self.assertEqual((reply["max_visual_frames"], reply["compact_to_visual_frames"]), (300, 100))
        _, reply = self.open(max_visual_frames=40)
        self.assertEqual(reply["compact_to_visual_frames"], 20)
        self.assertEqual([source.options["max_visual_frames"] for source in self.engine.sources], [300, 40])
        for body in ({"max_visual_frames": 701}, {"max_visual_frames": 300, "compact_to_visual_frames": 101},
                     {"max_visual_frames": 10, "compact_to_visual_frames": 11}):
            self.assertEqual(self.client.post("/v1/sessions", json=body).status_code, 422, body)

    def test_frames_one_a_second_pictures_only(self):
        root, _ = self.open()
        first = self.frame(root, 0.2).json()
        self.assertEqual((first["accepted"], first["health"]["status"]), (True, "ok"))
        self.assertEqual(self.frame(root, 0.9).json()["reason"], "live_1hz_sampling")
        self.assertTrue(self.frame(root, 1.0).json()["accepted"])
        self.assertEqual(self.frame(root, 2.0, b"not a picture").status_code, 415)
        self.assertEqual(self.frame(root, 2.0, b"\x89PNG" + bytes(16)).status_code, 415)
        self.assertEqual(self.frame(root, 2.0, JPEG + bytes(self.settings.policy.max_frame_bytes)).status_code, 413)
        self.assertTrue(self.frame(root, 2.0, b"\x89PNG\r\n\x1a\n" + bytes(16)).json()["accepted"])
        sent = self.engine.sources[0].parts("image_url")
        self.assertEqual([(part["timestamp_sec"], part["frame_index"]) for part in sent], [(0.2, 0), (1.0, 1), (2.0, 2)])
        # The system turn rides on the first frame only.
        self.assertEqual([[m["role"] for m in messages] for messages in self.engine.sources[0].sent],
                         [["system", "user"], ["user"], ["user"]])
        with patch.object(app_module, "MIN_FRAME_WALL_GAP_S", 5.):
            self.assertEqual(self.frame(root, 3.0).json()["reason"], "live_wall_rate")

    def test_a_question_about_now_goes_to_the_offline_model_with_the_newest_frame(self):
        root, _ = self.open()
        self.frame(root, 0.)
        row = self.answer(root, Q["table"], 0.5, time_scope="current")
        self.assertEqual((row["status"], row["route"], row["task"], row["backend"], row["text"], row["route_decided_by"]),
                         ("ok", "realtime", "realtime", "offline_model", A["cup"], "rule"))
        self.assertFalse({"task_template", "vision_backend", "harness_backend"} & set(row))
        content = self.bodies[-1]["messages"][-1]["content"]
        self.assertEqual([part["type"] for part in content], ["image_url", "text"])
        self.assertEqual(self.client.get(f"{root}/results", params={"after": 0}).json()["controls_seen"], 1)
        # The same question again sees the frame only, not the answer it got before.
        self.answer(root, Q["table"], 0.6, after=row["seq"], time_scope="current")
        self.assertEqual([m["role"] for m in self.bodies[-1]["messages"]], ["user"])

    def test_without_a_frame_a_question_about_now_says_so(self):
        root, _ = self.open()
        row = self.answer(root, Q["table"], 0.5, time_scope="current")
        self.assertEqual((row["status"], row["text"]), ("insufficient_evidence", say("no_frame_yet")))
        row = self.answer(root, "What is on the table?", 0.6, after=row["seq"], time_scope="current")
        self.assertEqual(row["text"], say("no_frame_yet", "en"))

    def test_the_past_is_answered_by_the_sessions_own_source(self):
        root, _ = self.open()
        self.frame(root, 0.)
        self.frame(root, 1.)
        self.engine.reply = A["picked_at_2"]
        row = self.answer(root, Q["past_held"], 1.5)
        self.assertEqual((row["route"], row["task"], row["text"], row["backend"], row["coverage"]),
                         ("past", "past", A["picked"], "streaming_session", "stream"))
        self.assertEqual(row["source_window_s"], [0.0, 1.0])
        asked = self.engine.sources[0].parts("text")
        self.assertEqual([(part["text"], part["timestamp_sec"]) for part in asked], [(Q["past_held"], 1.5)])
        self.assertFalse(any("force_response" in part for part in asked))
        self.assertEqual(self.chat.bodies, [])

    def test_a_silent_source_leaves_the_past_to_the_offline_model(self):
        root, _ = self.open()
        for t in range(3):
            self.frame(root, float(t))
        row = self.answer(root, "What did I hold?", 2.5, time_scope="past")
        self.assertEqual((row["route"], row["backend"], row["coverage"], row["fallback_reason"]),
                         ("past", "offline_model", "buffer", "stream_silent"))
        self.assertEqual(self.chat.images(), 3)

    def test_the_model_routes_what_the_rules_do_not_and_chat_keeps_its_conversation(self):
        root, _ = self.open()
        self.frame(root, 0.)
        self.chat.routes = ["chat", "chat"]
        first = self.answer(root, Q["who"], 0.5)
        self.assertEqual((first["route"], first["task"], first["route_decided_by"]), ("chat", "chat", "model"))
        self.answer(root, Q["again"], 0.6, after=first["seq"])
        messages = self.bodies[-1]["messages"]
        self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant", "user"])
        self.assertEqual((messages[1]["content"], messages[2]["content"]), (Q["who"], A["cup"]))

    def test_a_watch_is_left_in_the_source_reported_once_and_stopped_by_word_or_call(self):
        root, _ = self.open()
        self.frame(root, 0.)
        self.engine.reply = HOLD
        row = self.next_row(root, Q["watch_cat"], 0.5)
        self.assertEqual((row["status"], row["route"], row["task"], row["text"]),
                         ("watch_accepted", "watch", "watch", say("watch_accepted")))
        source = self.engine.sources[0]
        self.assertEqual(source.parts("text")[-1]["text"], Q["watch_cat"])
        self.client.portal.call(source.respond, source.held[-1], A["cat_at_9"], 9.)
        report = self.next_row(root, None, after=row["seq"])
        self.assertEqual((report["status"], report["text"], report["t"], report["route"], report["backend"]),
                         ("model_report", A["cat"], 9.0, "watch", "streaming_session"))
        self.assertEqual(self.client.delete(f"{root}/watch").json(), {"stopped": True})
        self.assertEqual(self.client.delete(f"{root}/watch").json(), {"stopped": False})
        accepted = self.next_row(root, Q["watch_dog"], 0.6, after=report["seq"])
        stopped = self.answer(root, Q["stop"], 0.7, after=accepted["seq"])
        self.assertEqual((stopped["route"], stopped["task"], stopped["text"]), ("cancel", "cancel", say("stopped")))
        stopped = self.answer(root, "Stop watching.", 0.8, after=stopped["seq"])
        self.assertEqual(stopped["text"], say("nothing_to_stop", "en"))

    def next_row(self, root, text, t=None, after=0):
        if text is not None:
            self.client.post(f"{root}/text", json={"text": text, "t": t})
        return self.next_answer(root, after)

    def test_what_a_client_gets_for_a_bad_request(self):
        root, reply = self.open()
        self.assertEqual(self.client.post(f"{root}/text", json={"text": Q["now"]}).status_code, 422)
        self.assertEqual(self.client.post(f"{root}/text", json={"text": "  ", "t": 1.0}).status_code, 422)
        self.assertEqual(self.client.post(f"{root}/text", json={"text": "x", "t": 1.0, "time_scope": "later"}).status_code, 422)
        self.assertEqual(self.client.post(f"{root}/video", content=b"x", headers={"Content-Type": "video/mp4"}).status_code, 409)
        self.assertEqual(self.client.get("/v1/sessions/nope/results").status_code, 404)
        for removed in ({"caption_bias": 2.0}, {"sampling": "uniform_rgb"}, {"sample_budget": 8},
                        {"max_responses": 3}, {"mode": "qa", "max_visual_frames": 5000}):
            self.assertEqual(self.client.post("/v1/sessions", json={"mode": "qa", **removed}).status_code, 422, removed)
        log = self.client.get(f"{root}/logs", headers={"X-Session-Log-Token": reply["logs_token"]})
        self.assertEqual(log.status_code, 200)
        self.assertEqual(log.headers["cache-control"], "no-store")
        self.assertIn("request_error", json.dumps(log.json()))
        self.assertEqual(self.client.get(f"{root}/logs").status_code, 404)
        self.assertEqual(self.client.get(f"{root}/logs", headers={"X-Session-Log-Token": "x"}).status_code, 404)

    def test_frame_and_question_times_are_bounded_and_any_positive_fps_is_taken(self):
        # Old logic: t=1e308 overflowed frame_index (t * fps) into a 500, and a t of 1e300 was taken
        # and locked its session out of every later frame. The port had also capped fps at 1000.
        root, _ = self.open(fps=30.0)
        for t in (1e308, 1e300):
            self.assertEqual(self.frame(root, t).status_code, 422, t)
        self.assertTrue(self.frame(root, 1.0).json()["accepted"])
        self.assertEqual(self.client.post(f"{root}/text", json={"text": Q["table"], "t": 1e308}).status_code, 422)
        root, reply = self.open(fps=1e305)
        self.assertEqual(reply["fps"], 1e305)
        # 1e4 s is a fair time, but at this rate its frame_index overflows.
        self.assertEqual(self.frame(root, 1e4).status_code, 422)
        self.assertEqual(self.frame(root, 0.).status_code, 200)

    def test_a_frame_index_beyond_64_bits_is_refused(self):
        # Old logic: only t * fps being finite was checked. At fps 1e305 a frame at t=1 reached the
        # engine with a 305-digit frame_index, which torch cannot hold, and the session lost its source.
        for fps, t in ((1e305, 1.), (1e9, 1e10)):
            root, _ = self.open(fps=fps)
            reply = self.frame(root, t)
            self.assertEqual((reply.status_code, reply.json()["detail"]),
                             (422, "frame timestamp out of range for the session's fps"), (fps, t))
            self.assertEqual(self.engine.sources[-1].parts("image_url"), [])
            self.assertTrue(self.frame(root, 0.).json()["accepted"])
            self.client.delete(root)
        # Unix time at a camera's rate fits.
        root, _ = self.open(fps=240.)
        self.assertTrue(self.frame(root, 1.8e9).json()["accepted"])
        self.assertEqual(self.engine.sources[-1].parts("image_url")[-1]["frame_index"], 432_000_000_000)

    def test_an_unhandled_error_still_names_the_version(self):
        async def broken():
            raise RuntimeError("broken")

        self.app.add_api_route("/broken", broken)
        reply = TestClient(self.app, raise_server_exceptions=False).get("/broken")
        self.assertEqual((reply.status_code, reply.headers.get("x-ov25-harness-version")), (500, __version__))

    def test_an_event_stream_that_starts_as_the_session_closes_ends(self):
        # Old logic: the stream subscribed when its body started, after the route's check. A close in
        # between had already told its subscribers, so this one waited for ever.
        root, _ = self.open()
        runtime = self.runtime(root)
        subscribe = runtime.subscribe

        def late():
            runtime.closing = True
            for queue in runtime._waiters:
                queue.put_nowait(None)
            return subscribe()

        runtime.subscribe = late
        try:
            with ThreadPoolExecutor(1) as pool:
                stream = pool.submit(lambda: self.client.get(f"{root}/events"))
                try:
                    reply = stream.result(5)
                finally:
                    self.client.delete(root)
        finally:
            runtime.subscribe = subscribe
        self.assertEqual((reply.status_code, reply.text), (200, ""))

    def test_the_event_stream_carries_every_row_until_the_session_closes(self):
        root, _ = self.open()
        self.frame(root, 0.)
        self.answer(root, Q["table"], 0.5, time_scope="current")
        self.engine.reply = A["picked_at_2"]
        with ThreadPoolExecutor(1) as pool:
            stream = pool.submit(lambda: self.client.get(f"{root}/events").text)
            time.sleep(.3)
            self.answer(root, Q["past_held"], 0.6, after=1)
            self.client.delete(root)
            rows = [json.loads(line[6:]) for line in stream.result(5).splitlines() if line.startswith("data: ")]
        self.assertEqual([(row["seq"], row["route"]) for row in rows], [(1, "realtime"), (2, "past")])

    def test_a_long_poll_returns_every_row_after_the_cursor_when_it_wakes(self):
        root, _ = self.open()
        runtime = self.runtime(root)
        with ThreadPoolExecutor(1) as pool:
            poll = pool.submit(lambda: self.client.get(f"{root}/results", params={"after": 0, "wait": 5}).json())
            time.sleep(.3)

            def two_rows():
                runtime.emit(dict(type="answer", t=1.0, text="first", status="model_report", route="watch", task="watch"))
                runtime.emit(dict(type="answer", t=1.0, text="second", status="ok", route="past", task="past"))

            self.client.portal.call(two_rows)
            reply = poll.result(5)
        self.assertEqual([row["text"] for row in reply["results"]], ["first", "second"])
        self.assertEqual(reply["cursor"], 2)
        # A poll that times out returns nothing, not an error.
        self.assertEqual(self.client.get(f"{root}/results", params={"after": 2, "wait": .2}).json()["results"], [])

    def test_a_caption_session_passes_the_models_own_captions_on(self):
        root, reply = self.open(mode="caption")
        self.assertEqual((reply["mode"], "logs_url" in reply), ("caption", False))
        options = self.engine.sources[0].options
        self.assertEqual((options["enable"], options["max_responses"]), (["caption"], 3))
        self.engine.caption = A["caption"]
        self.frame(root, 0.)
        rows = self.client.get(f"{root}/results", params={"after": 0, "wait": 2}).json()["results"]
        self.assertEqual([(row["type"], row["text"], row["t"]) for row in rows], [("caption", A["caption"], 0.0)])
        self.assertEqual(self.answer(root, Q["table"], 0.5, after=rows[-1]["seq"], time_scope="current")["status"], "ok")

    def test_native_events_are_passed_on_when_asked_for(self):
        root, reply = self.open(native_events=True)
        self.assertTrue(reply["native_events"])
        self.frame(root, 0.)
        rows = self.client.get(f"{root}/results", params={"after": 0, "wait": 2}).json()["results"]
        self.assertEqual([(row["type"], row["event"], row["control"], row["source"]) for row in rows],
                         [("native_event", "input.done", "silence", "observer")])


class StatusTests(GatewayTests):
    """The statuses the built-in routes produce when something fails. With the other tests here, every
    row type (answer, caption, warning, closed, error, native_event) and every status the code produces
    appears at least once; "cancelled" is in the API's list but nothing produces it."""

    def test_an_offline_model_that_fails_or_answers_nothing(self):
        root, _ = self.open()
        self.frame(root, 0.)
        self.chat.error = httpx.ConnectError("refused")
        row = self.answer(root, Q["table"], 0.5, time_scope="current")
        self.assertEqual((row["status"], row["text"]), ("upstream_unavailable", say("offline_model_no_answer")))
        self.assertNotIn("backend", row)
        self.chat.error, self.chat.answer = None, " "
        row = self.answer(root, Q["table"], 0.6, after=row["seq"], time_scope="current")
        self.assertEqual((row["status"], row["text"]), ("invalid_visual_format", say("no_usable_answer")))

    def test_a_watch_without_a_source(self):
        root, _ = self.open()
        self.frame(root, 0.)
        self.client.portal.call(lambda: setattr(self.runtime(root).session, "stream", None))
        row = self.answer(root, Q["watch_cat"], 0.5)
        self.assertEqual((row["status"], row["text"]), ("stream_unavailable", say("stream_unavailable_watch")))
        row = self.answer(root, Q["past_held"], 0.6, after=row["seq"])
        self.assertEqual((row["status"], row["text"]), ("stream_unavailable", say("stream_unavailable_past")))

    def test_an_error_row_when_the_source_fails_and_cannot_be_dropped(self):
        root, _ = self.open()
        runtime = self.runtime(root)
        self.client.portal.call(lambda: setattr(runtime.session, "stream", None))
        self.client.portal.call(lambda: self.engine.sources[0].queue.put_nowait("not an event"))
        end = time.monotonic() + 2
        while not runtime.results and time.monotonic() < end:
            time.sleep(.02)
        self.assertEqual([row["type"] for row in runtime.results], ["error"])
        # The session is failed from then on; the client hears why.
        failed = self.client.get(f"{root}/results")
        self.assertEqual((failed.status_code, failed.json()["detail"]), (409, runtime.failure))


class TimeoutTests(GatewayTests):
    policy = {"live_turn_s": .3}

    def test_a_question_that_outlives_its_turn_says_so(self):
        root, _ = self.open()
        self.frame(root, 0.)
        self.chat.delay = 5.
        row = self.answer(root, Q["table"], 0.5, time_scope="current")
        self.assertEqual((row["status"], row["text"], row["route"]), ("request_timeout", say("request_timeout"), None))


class HealthWarningTests(GatewayTests):
    def test_a_low_frame_rate_is_warned_in_the_language_of_the_last_question(self):
        root, _ = self.open()
        runtime = self.runtime(root)
        self.answer(root, Q["table"], 0.5, time_scope="current")
        # Frames came every 3 s for the last 21 s; the next one tells the session.
        health = FrameHealth(now=0., say=runtime.say)
        now = time.monotonic()
        for index in range(8):
            health.frame(3. * index - 30, now - 21 + 3 * index)
        runtime.frame_health = health
        self.frame(root, 1.)
        rows = self.client.get(f"{root}/results", params={"after": 1}).json()["results"]
        warning, = [row for row in rows if row["type"] == "warning"]
        self.assertEqual((warning["status"], warning["text"]),
                         ("low_frame_rate", say("low_frame_rate", window="10", frames=warning["frames_10s"])))


class IdleTests(GatewayTests):
    policy = {"live_idle_s": .3}

    def test_a_session_without_frames_is_closed_and_says_why(self):
        root, _ = self.open()
        self.frame(root, 0.)
        rows = self.client.get(f"{root}/results", params={"after": 0, "wait": 3}).json()["results"]
        self.assertEqual([(row["type"], row["reason"]) for row in rows], [("closed", "no_frames")])
        # Old logic: formatted with :.0f, a fractional live_idle_s of 0.3 was reported as 0 s.
        self.assertEqual(rows[0]["text"], say("closed_idle", "en", seconds="0.3"))
        time.sleep(.3)
        gone = self.frame(root, 5.)
        self.assertEqual(gone.status_code, 404)
        self.assertIn("no frame", gone.json()["detail"])
        # Closing it says so too.
        gone = self.client.delete(root)
        self.assertEqual((gone.status_code, gone.json()["detail"]), (404, "session closed: no frame for 0.3 s"))
        self.assertEqual(self.app.state.pool.held, 0)


class PoolTests(unittest.TestCase):
    def test_two_apps_have_their_own_stream_limits(self):
        first = build_app(settings(streaming={"user_streams": 1}), chat=Chat(), streaming=Engine())
        second = build_app(settings(streaming={"user_streams": 2}), chat=Chat(), streaming=Engine())
        self.assertIsNot(first.state.pool, second.state.pool)
        with TestClient(first) as one, TestClient(second) as two:
            self.assertEqual(one.post("/v1/sessions", json={}).status_code, 201)
            self.assertEqual(one.post("/v1/sessions", json={}).status_code, 503)
            for _ in range(2):
                self.assertEqual(two.post("/v1/sessions", json={}).status_code, 201)
            self.assertEqual(two.post("/v1/sessions", json={}).status_code, 503)
            self.assertEqual((first.state.pool.held, second.state.pool.held), (1, 2))
        self.assertEqual((first.state.pool.held, second.state.pool.held), (0, 0))


class RecordingTests(GatewayTests):
    def test_a_recording_is_uploaded_then_asked_about_through_the_subtask(self):
        reply = self.client.post("/v1/sessions", json={"input_mode": "offline", "fps": 25.0})
        self.assertEqual((reply.status_code, reply.json()["api_version"], self.engine.sources), (201, 1, []))
        self.assertNotIn("sampling", reply.json())
        root = "/v1/sessions/" + reply.json()["session_id"]
        self.assertEqual(self.client.post(f"{root}/text", json={"text": Q["video"]}).status_code, 409)
        self.assertEqual(self.frame(root, 0.).status_code, 409)
        self.assertEqual(self.client.delete(f"{root}/watch").status_code, 409)
        self.assertEqual(self.client.post(f"{root}/video", content=b"x", headers={"Content-Type": "text/plain"}).status_code, 415)
        seen = {}

        recording = Recording()

        async def inspect(path, **options):
            seen.update(options)
            return recording

        with patch.object(app_module.Recording, "inspect", inspect):
            uploaded = self.client.post(f"{root}/video", content=b"mp4 bytes", headers={"Content-Type": "video/mp4"})
        self.assertEqual((uploaded.status_code, uploaded.json()["source_frames"], uploaded.json()["source_fps"]), (201, 150, 25.0))
        media = self.settings.media
        self.assertEqual(seen, dict(ffprobe=media.ffprobe, ffmpeg=media.ffmpeg, max_duration_s=media.max_recording_s))
        # The decode starts with the upload, before any question.
        self.assertEqual(recording.decodes, [subtasks.video_plan(recording, self.settings.chat)])
        self.assertEqual(self.client.post(f"{root}/video", content=b"x", headers={"Content-Type": "video/mp4"}).status_code, 409)
        self.assertEqual(self.client.post(f"{root}/text", json={"text": Q["video"], "time_scope": "past"}).status_code, 422)
        self.assertEqual(self.client.post(f"{root}/text", json={"text": Q["after"], "t": 999.}).status_code, 422)
        row = self.answer(root, Q["video"])
        self.assertEqual((row["status"], row["route"], row["task"], row["backend"]), ("ok", "offline", "offline_direct", "offline_model"))
        self.assertEqual(row["source_window_s"], [0.0, 5.0])
        content = self.bodies[-1]["messages"][-1]["content"]
        self.assertEqual([part["type"] for part in content], ["text", "image_url"] * 3 + ["text"])
        self.assertEqual([part["text"] for part in content[:-1:2]], ["<0.0 seconds>", "<3.0 seconds>", "<5.0 seconds>"])
        row = self.answer(root, Q["video"], t=4., after=row["seq"])
        self.assertEqual(row["source_window_s"], [0.0, 3.0])
        self.assertEqual(self.client.delete(root).json()["closed"], True)
        self.assertTrue(recording.forgotten)

    def test_offline_sessions_are_limited(self):
        app = build_app(settings(media={"max_offline_sessions": 1}), chat=self.chat, streaming=self.engine)
        with TestClient(app) as client:
            self.assertEqual(client.post("/v1/sessions", json={"input_mode": "offline"}).status_code, 201)
            self.assertEqual(client.post("/v1/sessions", json={"input_mode": "offline"}).status_code, 503)


class Speaker:
    rate = 24000

    async def __call__(self, text):
        tone = (8000 * np.sin(2 * np.pi * 440 * np.arange(24000) / 24000)).astype("<i2").tobytes()
        for at in range(0, len(tone), 8192):
            yield tone[at:at + 8192]


class Transcriber:
    async def __call__(self, audio, rate):
        return Q["table"]


class VoiceTests(GatewayTests):
    voice = True

    def until(self, ws, kind):
        """Messages up to the first of a kind, and the audio bytes that came with them."""
        seen, audio = [], b""
        while True:
            message = ws.receive()
            if message.get("bytes") is not None:
                audio += message["bytes"]
                continue
            seen.append(json.loads(message["text"]))
            if seen[-1]["type"] == kind:
                return seen, audio

    def test_a_typed_question_is_spoken_at_the_rate_the_client_asked_for(self):
        self.assertTrue(self.client.get("/healthz").json()["voice"])
        for rate, samples in ((16000, 16000), (24000, 24000)):
            root, reply = self.open(voice=True)
            self.assertIn("audio_url", reply)
            self.frame(root, 0.)
            with self.client.websocket_connect(f"{root}/audio") as ws:
                ws.send_text(json.dumps({"type": "hello", "rate": 16000, "out_rate": rate, "vad": "client"}))
                ready = json.loads(ws.receive_text())
                self.assertEqual((ready["type"], ready["out_rate"], ready["tts_rate"]), ("ready", rate, 24000))
                self.client.post(f"{root}/text", json={"text": Q["table"], "t": 0.5, "time_scope": "current"})
                seen, audio = self.until(ws, "audio.end")
                self.assertEqual([m["type"] for m in seen], ["answer", "audio.begin", "audio.end"])
                self.assertEqual((seen[0]["route"], seen[0]["task"]), ("realtime", "realtime"))
                self.assertEqual((seen[1]["rate"], seen[1]["text"]), (rate, A["cup"]))
                self.assertEqual((len(audio) // 2, seen[2]["samples"], seen[2]["ms"]), (samples, samples, 1000))
                level = np.abs(np.frombuffer(audio, "<i2").astype(float)[samples // 4: -samples // 4]).max()
                self.assertAlmostEqual(level / 8000, 1.0, delta=.02)
                ws.send_text(json.dumps({"type": "bye"}))
            self.client.delete(root)

    def test_a_spoken_question_is_transcribed_and_answered_and_remembered_as_heard(self):
        root, _ = self.open(voice=True)
        self.frame(root, 0.)
        self.chat.routes = ["realtime", "chat"]
        with self.client.websocket_connect(f"{root}/audio") as ws:
            ws.send_text(json.dumps({"type": "hello", "rate": 16000, "out_rate": 24000, "vad": "client", "t": 0.5}))
            ws.receive_text()
            ws.send_text(json.dumps({"type": "speech.begin", "t": 0.5}))
            ws.send_bytes(bytes(16000))
            ws.send_text(json.dumps({"type": "speech.end", "t": 0.9}))
            seen, _ = self.until(ws, "audio.end")
            self.assertEqual([m["type"] for m in seen], ["transcript", "answer", "audio.begin", "audio.end"])
            self.assertEqual((seen[0]["text"], seen[1]["route"]), (Q["table"], "realtime"))
            ws.send_text(json.dumps({"type": "play.done", "speech_id": seen[2]["speech_id"]}))
            self.client.post(f"{root}/text", json={"text": Q["again"], "t": 0.95})
            self.until(ws, "audio.end")
            ws.send_text(json.dumps({"type": "bye"}))
        messages = self.bodies[-1]["messages"]
        self.assertEqual([(m["role"], m["content"]) for m in messages[1:3]], [("user", Q["table"]), ("assistant", A["cup"])])

    def test_a_past_answer_is_committed_to_the_stream_after_playback(self):
        root, _ = self.open(voice=True)
        self.frame(root, 0.)
        source = self.engine.sources[0]
        self.engine.reply = "At 0 seconds: A cup."
        with self.client.websocket_connect(f"{root}/audio") as ws:
            ws.send_text(json.dumps({"type": "hello"}))
            ws.receive_text()
            reply = self.client.post(f"{root}/text", json={"text": "What did I see?", "t": .5, "time_scope": "past"})
            self.assertEqual(reply.status_code, 200)
            seen, _ = self.until(ws, "audio.end")
            self.assertEqual((seen[0]["route"], seen[0]["text"]), ("past", "A cup."))
            self.assertEqual(source.committed, [])
            ws.send_text(json.dumps({"type": "play.done", "speech_id": seen[-1]["speech_id"]}))
            ws.send_text(json.dumps({"type": "bye"}))
            self.until(ws, "closed")
        self.frame(root, 1.)
        row = self.client.get(f"{root}/results").json()["results"][0]
        self.assertEqual(source.committed, [(row["native_receipt"]["qa_response_id"], "A cup.")])

    def test_bad_messages_on_the_audio_socket(self):
        root, _ = self.open(voice=True)
        with self.client.websocket_connect(f"{root}/audio") as ws:
            ws.send_text("[]")
            self.assertEqual(ws.receive()["code"], 4422)
        with self.client.websocket_connect(f"{root}/audio") as ws:
            ws.send_text(json.dumps({"type": "hello", "rate": 4000}))
            self.assertEqual(ws.receive()["code"], 4422)
        with self.client.websocket_connect(f"{root}/audio") as ws:
            ws.send_text(json.dumps({"type": "hello"}))
            ws.receive_text()
            ws.send_text("[1]")
            self.assertEqual(json.loads(ws.receive_text())["detail"], "not a JSON object")
            ws.send_text(json.dumps({"type": "speech.begin", "t": 0.5}))
            for _ in range(31):
                ws.send_bytes(bytes(32000))
            error = json.loads(ws.receive_text())
            self.assertEqual(error["detail"], LOCALE.message("en", "voice_too_long"))
            ws.send_text(json.dumps({"type": "bye"}))

    def test_bad_numbers_on_the_audio_socket_leave_it_usable(self):
        # Old logic: float() of a speech.end t or a cancel played_ms that was not a number raised out of
        # the socket's loop and killed it without a word; a NaN hello t was taken and made every later
        # question invalid.
        root, _ = self.open(voice=True)
        self.frame(root, 0.)
        with self.client.websocket_connect(f"{root}/audio") as ws:
            ws.send_text('{"type": "hello", "t": NaN}')
            self.assertEqual(ws.receive()["code"], 4422)
        with self.client.websocket_connect(f"{root}/audio") as ws:
            ws.send_text(json.dumps({"type": "hello", "rate": 16000, "vad": "client", "t": 0.5}))
            ws.receive_text()
            ws.send_text(json.dumps({"type": "speech.begin", "t": 0.5}))
            ws.send_bytes(bytes(16000))
            ws.send_text(json.dumps({"type": "speech.end", "t": "abc"}))
            self.assertEqual(json.loads(ws.receive_text()),
                             {"type": "error", "detail": "t must be a number from 0 to 10^10", "fatal": False})
            ws.send_text(json.dumps({"type": "cancel", "played_ms": "x"}))
            self.assertEqual(json.loads(ws.receive_text())["detail"], "played_ms must be a finite number")
            ws.send_text(json.dumps({"type": "speech.begin", "t": 0.5}))
            ws.send_bytes(bytes(16000))
            ws.send_text(json.dumps({"type": "speech.end", "t": 0.9}))
            seen, _ = self.until(ws, "transcript")
            self.assertEqual((seen[-1]["t"], seen[-1]["text"]), (0.9, Q["table"]))
            ws.send_text(json.dumps({"type": "bye"}))

    def test_voice_times_are_bounded_as_http_ones_are(self):
        # Old logic: hello t and speech.end t only had to be finite. POST /text refused t=1e300, the
        # voice socket took it, and the question reached the session (and the engine) at 1e300 s.
        root, _ = self.open(voice=True)
        self.frame(root, 0.)
        self.assertEqual(self.client.post(f"{root}/text", json={"text": Q["table"], "t": 1e300}).status_code, 422)
        for t in (1e300, 1.7e308, -1.):
            with self.client.websocket_connect(f"{root}/audio") as ws:
                ws.send_text(json.dumps({"type": "hello", "t": t}))
                self.assertEqual(ws.receive()["code"], 4422, t)
        with self.client.websocket_connect(f"{root}/audio") as ws:
            ws.send_text(json.dumps({"type": "hello", "rate": 16000, "vad": "client", "t": 0.5}))
            ws.receive_text()
            for t in (1e300, -1.):
                ws.send_text(json.dumps({"type": "speech.begin", "t": 0.5}))
                ws.send_bytes(bytes(16000))
                ws.send_text(json.dumps({"type": "speech.end", "t": t}))
                self.assertEqual(json.loads(ws.receive_text())["detail"], "t must be a number from 0 to 10^10")
            ws.send_text(json.dumps({"type": "bye"}))
        self.assertEqual([row for row in self.runtime(root).results if row.get("type") == "answer"], [])

    def test_a_server_vad_turn_of_almost_30_s_of_speech_is_taken(self):
        # Old logic: the 30 s cap also counted the second kept from before detection and the 0.8 s
        # of silence that ends the turn, so server VAD dropped turns of about 28.2 s of speech.
        rate = 16000
        noise = np.random.default_rng(0).normal(0, 100, rate * 40).astype("<i2")
        tone = (8000 * np.sin(2 * np.pi * 220 * np.arange(rate * 8 // 100) / rate)).astype("<i2")

        def packets(seconds, speech):
            for index in range(round(seconds * 10)):
                quiet = noise[index * 1600:(index + 1) * 1600]
                # 80 ms of voice and a 20 ms dip in every 100 ms packet
                yield np.concatenate([tone, quiet[:320]]).tobytes() if speech else quiet.tobytes()

        root, _ = self.open(voice=True)
        self.frame(root, 0.)
        with self.client.websocket_connect(f"{root}/audio") as ws:
            ws.send_text(json.dumps({"type": "hello", "rate": rate, "vad": "server"}))
            ws.receive_text()
            for seconds, speech in ((3, False), (29.5, True), (2, False)):
                for packet in packets(seconds, speech):
                    ws.send_bytes(packet)
            while (message := json.loads(ws.receive_text()))["type"] not in ("transcript", "error"):
                pass
            self.assertEqual((message["type"], message.get("text")), ("transcript", Q["table"]))
            ws.send_text(json.dumps({"type": "bye"}))

    def test_server_vad_keeps_one_second_between_turns_and_caps_a_turn(self):
        # Old logic: with server VAD every packet was kept while nobody spoke (about 345 MB an hour at
        # 48 kHz), and a turn had no length limit.
        sizes = []

        class Buffer(bytearray):
            def extend(self, data):
                super().extend(data)
                sizes.append(len(self))

        class Detector:
            """Speech is any packet that is not silence."""

            hangover_ms = 0

            def __init__(self, rate):
                self.speaking = False

            def __call__(self, pcm):
                if any(pcm) == self.speaking:
                    return []
                self.speaking = not self.speaking
                return ["begin" if self.speaking else "end"]

        root, _ = self.open(voice=True)
        with (patch.object(audio_module, "bytearray", Buffer, create=True),
              patch.object(audio_module.pcmlib, "EnergyVAD", Detector),
              self.client.websocket_connect(f"{root}/audio") as ws):
            ws.send_text(json.dumps({"type": "hello", "rate": 16000, "vad": "server"}))
            ws.receive_text()
            for _ in range(100):
                ws.send_bytes(bytes(3200))   # 10 s of silence
            ws.send_text(json.dumps({"type": "hello"}))
            self.assertEqual(json.loads(ws.receive_text())["detail"], "already said hello")
            self.assertLessEqual(max(sizes), 16000 * 2 + 3200)
            for _ in range(32):
                ws.send_bytes(b"\x10\x00" * 16000)   # 32 s of speech
            seen, _ = self.until(ws, "error")
            self.assertEqual([m["type"] for m in seen], ["speech.detected", "error"])
            self.assertEqual(seen[-1]["detail"], LOCALE.message("en", "voice_too_long"))
            # The end of a dropped turn is no question.
            ws.send_bytes(bytes(3200))
            ws.send_text(json.dumps({"type": "hello"}))
            seen, _ = self.until(ws, "error")
            self.assertEqual([m.get("detail") for m in seen], ["already said hello"])
            ws.send_text(json.dumps({"type": "bye"}))

    def test_a_session_opened_without_voice_has_no_audio_socket(self):
        root, _ = self.open()
        with self.client.websocket_connect(f"{root}/audio") as ws:
            self.assertEqual(ws.receive()["code"], 4400)


class NoVoiceTests(GatewayTests):
    def test_a_service_without_voice_backends_refuses_the_audio_socket(self):
        root, _ = self.open(voice=True)
        with self.client.websocket_connect(f"{root}/audio") as ws:
            self.assertEqual(ws.receive()["code"], 4500)


if __name__ == "__main__":
    unittest.main()
