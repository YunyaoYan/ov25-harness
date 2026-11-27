"""Smoke tests of the app with tiny inline backends: a session from open to close, the past route
answered by the stream and by the buffer fallback, the two fixed bugs, and configuration checks."""
import asyncio
import json
from pathlib import Path
import unittest
from unittest import mock
import uuid

import httpx
from ov_streaming.types import Event
from pydantic import ValidationError

from ov25_harness import __version__, app as app_module, config, router, runtime as runtime_module
from ov25_harness.app import build_app
from ov25_harness.backends import OpenAIChat
from ov25_harness.locale import Locale
from ov25_harness.stream import StreamPool, open_user_stream

ROOT = Path(__file__).resolve().parents[1]
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


class Source:
    """An engine session: frames are silent; a question is answered with owner.answer at once, left
    silent when it is None, or held (no decision) while owner.hold is set."""

    def __init__(self, owner, **options):
        self.owner, self.options = owner, options
        self.session_id = "src-" + uuid.uuid4().hex[:8]
        self.queue = asyncio.Queue()
        self.sent, self.held, self.cancelled = [], [], []
        self.committed = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.queue.put_nowait(None)

    async def abort(self):
        self.queue.put_nowait(None)

    async def cancel_response(self, response_id):
        self.cancelled.append(response_id)
        return True

    async def commit_answer(self, response_id, text):
        self.committed.append((response_id, text))
        return True

    def emit(self, kind, input_id, **fields):
        self.queue.put_nowait(Event(kind, self.session_id, input_id=input_id, **fields))

    def respond(self, input_id, text):
        response_id = "resp-" + uuid.uuid4().hex[:8]
        self.emit("input.done", input_id, response_id=response_id, control="response")
        self.emit("response.created", input_id, response_id=response_id)
        self.emit("response.output_text.done", input_id, response_id=response_id, text=text, control="response")
        self.emit("response.done", input_id, response_id=response_id, status="completed")
        return response_id

    async def send_messages(self, *, messages, input_id=None):
        input_id = input_id or uuid.uuid4().hex
        self.sent.append(messages)
        part = messages[-1]["content"][0]
        if part["type"] == "image_url":
            self.emit("input.done", input_id, control="silence")
        elif self.owner.hold:
            self.held.append(input_id)
        elif self.owner.answer is None:
            self.emit("input.done", input_id, control="silence")
        else:
            self.respond(input_id, self.owner.answer)
        return input_id

    async def events(self):
        while (event := await self.queue.get()) is not None:
            yield event


class Streaming:
    def __init__(self):
        self.answer, self.hold, self.sources = "At 2 seconds: a red cup.", False, []

    def session(self, **options):
        self.sources.append(Source(self, **options))
        return self.sources[-1]


class Chat:
    """The offline model: routes everything to `route`, answers with the number of pictures."""

    def __init__(self, route="realtime"):
        self.route, self.bodies = route, []

    async def complete(self, body, timeout):
        self.bodies.append(body)
        if "response_format" in body:
            return json.dumps({"route": self.route}), "stop"
        images = sum(part.get("type") == "image_url" for message in body["messages"]
                     if isinstance(message["content"], list) for part in message["content"])
        return f"saw {images} pictures", "stop"


class AppCase(unittest.IsolatedAsyncioTestCase):
    policy = {}

    async def asyncSetUp(self):
        self.settings = config.Settings.model_validate({"server": {"languages": ["en", "zh"]}, "policy": self.policy})
        self.streaming, self.chat = Streaming(), Chat()
        self.app = build_app(self.settings, chat=self.chat, streaming=self.streaming)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")
        patcher = mock.patch.object(app_module, "MIN_FRAME_WALL_GAP_S", 0.)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.app.state.harness.close()

    async def open(self, frames=3):
        r = await self.client.post("/v1/sessions", json={"fps": 1.0})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.headers["x-ov25-harness-version"], __version__)
        reply = r.json()
        self.assertEqual(reply["api_version"], 1)
        self.root, self.cursor = f"/v1/sessions/{reply['session_id']}", 0
        for t in range(frames):
            r = await self.client.post(f"{self.root}/frame", params={"t": t}, content=JPEG,
                                       headers={"Content-Type": "image/jpeg"})
            self.assertTrue(r.json()["accepted"], r.text)
        return reply

    async def ask(self, text, t, **body):
        r = await self.client.post(f"{self.root}/text", json={"text": text, "t": t, **body})
        self.assertEqual(r.status_code, 200, r.text)
        for _ in range(20):
            r = await self.client.get(f"{self.root}/results", params={"after": self.cursor, "wait": 5})
            rows = r.json()["results"]
            self.cursor = max([self.cursor, *(row["seq"] for row in rows)])
            answers = [row for row in rows if row["type"] == "answer" and row["status"] != "watch_accepted"]
            if answers:
                return answers[0]
        self.fail("no answer")

    async def close(self):
        r = await self.client.delete(self.root)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["closed"])
        self.assertEqual(self.app.state.pool.held, 0)


class SessionTest(AppCase):
    async def test_open_frame_question_results_close(self):
        reply = await self.open()
        self.assertEqual(reply["max_visual_frames"], 150)
        self.assertEqual(self.streaming.sources[0].options["answer_commit"], "client")
        row = await self.ask("What is in front of me?", 2, time_scope="current")
        self.assertEqual((row["route"], row["task"], row["backend"], row["status"]),
                         ("realtime", "realtime", "offline_model", "ok"))
        self.assertEqual(row["text"], "saw 1 pictures")
        self.assertEqual(row["source_window_s"], [2.0, 2.0])
        self.assertNotIn("task_template", row)
        health = (await self.client.get("/healthz")).json()
        self.assertEqual((health["version"], health["api_version"], health["sessions"]), (__version__, 1, 1))
        await self.close()
        self.assertEqual((await self.client.get(f"{self.root}/results")).status_code, 404)

    async def test_unresponsive_source_does_not_hold_the_close_open(self):
        await self.open(frames=0)
        source = self.streaming.sources[0]

        async def stall():
            await asyncio.Event().wait()

        source.abort = mock.AsyncMock(side_effect=stall)
        with mock.patch.object(runtime_module, "SOURCE_CLOSE_S", .05):
            reply = await asyncio.wait_for(self.client.delete(self.root), 1)
        source.abort.assert_awaited_once()
        self.assertEqual((reply.status_code, reply.json()["detail"]), (503, "session cleanup unconfirmed"))
        self.assertEqual(self.app.state.pool.held, 0)
        self.assertEqual(self.app.state.sessions, {})

    async def test_removed_open_options_are_refused(self):
        r = await self.client.post("/v1/sessions", json={"caption_bias": 1.0})
        self.assertEqual(r.status_code, 422)

    async def test_png_needs_its_whole_signature(self):
        await self.open(frames=0)
        r = await self.client.post(f"{self.root}/frame", params={"t": 0}, content=b"\x89PNG" + b"\x00" * 16)
        self.assertEqual(r.status_code, 415)
        await self.close()

    async def test_past_answered_by_the_stream(self):
        await self.open()
        row = await self.ask("Earlier, what was on the table?", 2)
        self.assertEqual((row["route"], row["task"], row["backend"], row["coverage"]),
                         ("past", "past", "streaming_session", "stream"))
        self.assertEqual(row["route_decided_by"], "rule")
        self.assertEqual(row["text"], "a red cup.")
        self.assertEqual(row["source_window_s"], [0.0, 2.0])
        # Asked as an ordinary question, after the newest frame.
        source = self.streaming.sources[0]
        part = source.sent[-1][-1]["content"][0]
        self.assertEqual((part["type"], part["timestamp_sec"]), ("text", 2.0))
        self.assertNotIn("force_response", part)
        await self.client.post(f"{self.root}/frame", params={"t": 3}, content=JPEG)
        self.assertEqual(source.committed, [(row["native_receipt"]["qa_response_id"], "a red cup.")])
        await self.close()
        self.assertFalse(source.cancelled)

    async def test_past_falls_back_to_the_buffer_when_the_stream_is_silent(self):
        self.streaming.answer = None
        await self.open()
        row = await self.ask("What did I hold?", 2, time_scope="past")
        self.assertEqual((row["route"], row["backend"], row["coverage"], row["fallback_reason"]),
                         ("past", "offline_model", "buffer", "stream_silent"))
        self.assertEqual(row["text"], "saw 3 pictures")
        self.assertEqual(row["source_window_s"], [0.0, 2.0])
        # The fallback asks the question unchanged and without the dialogue.
        body = self.chat.bodies[-1]
        self.assertEqual([message["role"] for message in body["messages"]], ["user"])
        self.assertEqual(body["messages"][0]["content"][-1]["text"], "What did I hold?")
        await self.close()

    async def test_compaction_moves_the_window_start(self):
        await self.open(frames=5)
        runtime = next(iter(self.app.state.sessions.values()))
        source = self.streaming.sources[0]
        source.emit("context.compacted", None, metadata={"retained_turns": 2, "retained_visual_frames": 2})
        await asyncio.sleep(0.05)
        self.assertEqual(runtime.session.stream.window_start(), 3.0)
        row = await self.ask("Earlier, what was on the table?", 4)
        self.assertEqual(row["source_window_s"], [3.0, 4.0])
        await self.close()


class LateAnswerTest(AppCase):
    # A turn so short the stream's share of it runs out at once.
    policy = {"live_turn_s": 2}

    async def test_answer_after_a_timed_out_past_question_is_not_a_watch_alert(self):
        self.streaming.answer = None
        await self.open()
        accepted = await self.client.post(f"{self.root}/text", json={"text": "Let me know when you see a dog.", "t": 2})
        self.assertEqual(accepted.status_code, 200)
        runtime = next(iter(self.app.state.sessions.values()))
        for _ in range(50):
            if runtime.session.stream.watching is not None:
                break
            await asyncio.sleep(0.02)
        self.assertIsNotNone(runtime.session.stream.watching)
        source = self.streaming.sources[0]
        self.streaming.hold = True
        row = await self.ask("What was on the table?", 2, time_scope="past")
        self.assertEqual(row["status"], "stream_unavailable")
        # The engine answers the abandoned question late.
        response_id = source.respond(source.held[-1], "a cup on the table")
        await asyncio.sleep(0.1)
        rows = [row for row in runtime.results if row.get("route") == "watch" and row["status"] == "model_report"]
        self.assertEqual(rows, [])
        self.assertIsNotNone(runtime.session.stream.watching)
        self.assertIn(response_id, source.cancelled)
        await self.close()


class StreamTimeoutTest(AppCase):
    # The stream gets 1.5 s of the turn, and the offline model the rest.
    policy = {"live_turn_s": 4.5}

    async def test_a_stream_that_never_decides_falls_back_to_the_buffer(self):
        await self.open()
        self.streaming.hold = True
        row = await self.ask("What was on the table?", 2, time_scope="past")
        self.assertEqual((row["status"], row["backend"], row["coverage"], row["fallback_reason"]),
                         ("ok", "offline_model", "buffer", "stream_timeout"))
        self.assertEqual(row["text"], "saw 3 pictures")
        await self.close()


class RoutingTimeoutTest(AppCase):
    policy = {"route_timeout_s": .05}

    async def test_the_router_falls_back_when_the_chat_queue_uses_its_budget(self):
        chat = OpenAIChat(self.settings)
        self.addAsyncCleanup(chat.aclose)
        harness = self.app.state.harness
        harness.chat = chat
        deadline = asyncio.get_running_loop().time() + 1
        async with chat.slots:
            # Exhaust every slot, as concurrent answers would.
            for _ in range(self.settings.chat.max_concurrency - 1):
                await chat.slots.acquire()
            try:
                route = await asyncio.wait_for(harness.route("How are you?", None, deadline), .5)
            finally:
                for _ in range(self.settings.chat.max_concurrency - 1):
                    chat.slots.release()
        self.assertEqual(route, ("realtime", "fallback"))


class PoolTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_open_gives_back_its_slot(self):
        started = asyncio.Event()

        class Stuck:
            session_id = "stuck"

            async def __aenter__(self):
                started.set()
                await asyncio.sleep(60)

        class Client:
            def session(self, **options):
                return Stuck()

        pool = StreamPool(1)
        task = asyncio.create_task(open_user_stream(pool, Client(), audit=lambda *a, **k: None, enable=[]))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(pool.held, 0)


class RulesTest(unittest.TestCase):
    def test_rules_of_both_languages(self):
        locale = Locale(["en", "zh"])
        self.assertEqual(router.by_rule("Stop watching.", locale), "cancel")
        self.assertEqual(router.by_rule("\u505c\u6b62\u89c2\u5bdf", locale), "cancel")
        self.assertEqual(router.by_rule("Let me know when you see a dog.", locale), "watch")
        self.assertIsNone(router.by_rule("Let me know when you see the dog again.", locale))
        self.assertEqual(router.by_rule("Just now, who came in?", locale), "past")
        self.assertIsNone(router.by_rule("What is this?", locale))
        self.assertEqual(locale.pick("\u73b0\u5728\u6709\u4ec0\u4e48"), "zh")
        self.assertEqual(locale.pick("What now?"), "en")
        self.assertEqual(Locale(["en"]).pick("\u73b0\u5728"), "en")
        self.assertIn("{age}", locale.tables["zh"]["messages"]["stale_frame"])
        self.assertEqual(set(locale.tables["en"]["check"]), set(locale.tables["zh"]["check"]))


class ConfigTest(unittest.TestCase):
    def test_shipped_configs_load(self):
        self.assertEqual(config.load(ROOT / "configs" / "local-fake.toml").streaming.kind, "fake")
        settings = config.load(ROOT / "configs" / "ov25.toml")
        self.assertEqual((settings.streaming.url, settings.chat.url),
                         ("http://127.0.0.1:8011", "http://127.0.0.1:8000/v1"))

    def test_bad_values_fail_with_the_field_name(self):
        for data, field in [({"policy": {"live_turn_s": 1000}}, "live_turn_s"),
                            ({"server": {"languages": ["fr"]}}, "languages"),
                            ({"chat": {"modle": "x"}}, "modle"),
                            ({"chat": {"url": "http://user:secret@127.0.0.1:8000/v1"}}, "url"),
                            ({"streaming": {"compact_to_visual_frames": 90, "max_visual_frames": 80}}, "streaming"),
                            ({"rewrite": {"enabled": True}}, "rewrite")]:
            with self.subTest(field=field), self.assertRaises(ValidationError) as caught:
                config.Settings.model_validate(data)
            self.assertIn(field, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
