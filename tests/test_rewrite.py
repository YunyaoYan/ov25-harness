"""The rewrite subtask: a text model tidies the question, the offline model answers it; a whole
recording goes to the offline model as one video. Also how a subtask is loaded."""
import asyncio
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

import httpx

from ov25_harness import rewrite, subtask as subtasks, video_input
from ov25_harness.backends import OpenAIChat
from ov25_harness.harness import Harness

from .support import LOCALE, ZH, Chat, settings

FRAME = dict(png=b"\xff\xd8\xff fake", pts=4.0, frame_index=4, fps=1.0)
TEXT_URL = "http://127.0.0.1:9/v1/chat/completions"
MODEL_URL = "http://127.0.0.1:8000/v1/chat/completions"
KEY_ENV = "OV25_TEST_REWRITE_KEY"
R = ZH["rewrite"]
ON = {KEY_ENV: "k"}


class Recording:
    end, fps, size = 20.0, 25.0, (1280, 720)

    def __init__(self, path=None):
        self.path, self.sampled = path, []

    def seconds(self, rate, size):
        self.sampled.append((rate, size))

        async def frames():
            return [dict(FRAME, pts=float(i), timestamp=float(i), frame_index=i) for i in (0, 10, 20)]
        return asyncio.ensure_future(frames())


class Host:
    """What the subtask uses of the harness: settings, the offline model (whose video() posts the
    whole recording), an HTTP client for the text model, direct() and the locale."""

    def __init__(self, rewrite=None, video=None, **config):
        self.rewrite, self.video = rewrite, video
        self.settings = settings(rewrite={"enabled": True, "url": TEXT_URL, "model": "text-model",
                                          "api_key_env": KEY_ENV, **config})
        self.locale, self.say = LOCALE, LOCALE.message
        self.text_requests, self.video_requests, self.direct_calls = [], [], []
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.reply))
        self.chat = OpenAIChat(self.settings)
        self.chat.http = httpx.AsyncClient(transport=httpx.MockTransport(self.reply))

    async def reply(self, request):
        if str(request.url) == TEXT_URL:
            self.text_requests.append((json.loads(request.content), request.headers.get("authorization")))
            if callable(self.rewrite):
                return await self.rewrite(request)
            content = json.dumps({"question": self.rewrite}, ensure_ascii=False)
            return httpx.Response(200, json={"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})
        self.video_url = str(request.url)
        self.video_requests.append(json.loads(await request.aread()))
        if callable(self.video):
            return self.video(self.video_requests[-1])
        return httpx.Response(200, json={"choices": [{"message": {"content": ZH["answers"]["cards"]}, "finish_reason": "stop"}]})

    async def direct(self, question, frames, dialogue, deadline):
        self.direct_calls.append((question, list(frames), tuple(dialogue)))
        return dict(text=ZH["answers"]["cup_short"], status="ok", backend="offline_model", finish_reason="stop")

    async def aclose(self):
        await self.http.aclose()
        await self.chat.aclose()


def request(question=R["question"], mode="live", route="realtime", **changes):
    base = dict(question=question, asked_at=4.0, input_mode=mode, frames=(FRAME,), current_frame=FRAME,
                recording=None, max_pixels=331776, dialogue=((R["asked_before"], R["answered_before"]),),
                route=route, deadline=time.monotonic() + 14)
    return subtasks.OfflineRequest(**{**base, **changes})


async def run_with(host, req, env=ON):
    with patch.dict(os.environ, env, clear=False):
        if KEY_ENV not in env:
            os.environ.pop(KEY_ENV, None)
        subtask = rewrite.factory(host)
    try:
        return await subtask.run(req)
    finally:
        await host.aclose()


class RewriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_rewritten_question_goes_to_the_model_with_the_current_frame_and_no_dialogue(self):
        host = Host(R["rewritten"])
        result = await run_with(host, request())
        self.assertEqual(host.direct_calls[0][0], R["rewritten"])
        self.assertEqual(host.direct_calls[0][1], [FRAME])
        self.assertEqual((result.text, result.status, result.task), (ZH["answers"]["cup_short"], "ok", "offline_rewrite"))
        self.assertEqual(result.fields["rewrite_status"], "rewritten")
        self.assertEqual(result.fields["rewritten_question"], R["rewritten"])
        body, key = host.text_requests[0]
        self.assertEqual(key, "Bearer k")
        self.assertEqual(body["model"], "text-model")
        sent = json.loads(body["messages"][-1]["content"])
        self.assertEqual(sent["question"], R["question"])
        self.assertEqual(sent["dialogue"], [[R["asked_before"], R["answered_before"]]])

    async def test_options_and_the_answer_format_stay_as_the_user_wrote_them(self):
        question = "What is he holding?\nA. a cup\nB. a pen\nAnswer with the option letter only."
        host = Host("What is the man in the red shirt holding?")
        req = request(question, dialogue=(("Who is at the table?", "A man in a red shirt."),))
        result = await run_with(host, req)
        self.assertEqual(host.direct_calls[0][0], "What is the man in the red shirt holding?\n"
                         "A. a cup\nB. a pen\nAnswer with the option letter only.")
        sent = json.loads(host.text_requests[0][0]["messages"][-1]["content"])
        self.assertEqual((sent["question"], sent["multiple_choice"]), ("What is he holding?", True))
        self.assertNotIn("a cup", host.text_requests[0][0]["messages"][-1]["content"])
        self.assertEqual(result.fields["rewrite_status"], "rewritten")

    async def test_a_rewrite_that_adds_a_number_an_option_or_another_language_is_dropped(self):
        question = "What is he holding?\nA. a cup\nB. a pen"
        for rewritten in ("What is he holding at 12 seconds?", "Is he holding a cup?", R["question"],
                          "What is he holding?\nLook closely.", "Describe the picture.", "", None, 7):
            host = Host(rewritten)
            result = await run_with(host, request(question, dialogue=()))
            self.assertEqual(host.direct_calls[0][0], question, rewritten)
            self.assertEqual(result.fields["rewrite_status"], "rejected", rewritten)

    async def test_a_rewrite_that_asks_something_else_is_dropped(self):
        host = Host(R["scene"])
        result = await run_with(host, request(R["who"], dialogue=()))
        self.assertEqual(host.direct_calls[0][0], R["who"])
        self.assertEqual(result.fields["rewrite_status"], "rejected")

    async def test_a_clear_question_comes_back_unchanged(self):
        host = Host(R["question_bare"])
        result = await run_with(host, request())
        self.assertEqual(host.direct_calls[0][0], R["question"])
        self.assertEqual(result.fields["rewrite_status"], "unchanged")
        self.assertNotIn("rewritten_question", result.fields)

    async def test_a_failed_or_slow_text_model_leaves_the_question_as_asked(self):
        async def slow(_):
            await asyncio.sleep(30)

        async def refused(_):
            return httpx.Response(401, json={"error": "no key"})

        async def unreachable(_):
            raise httpx.ConnectError("no route")

        async def garbled(_):
            return httpx.Response(200, json={"choices": [{"message": {"content": "Sure! Here is the question"}}]})

        for behaviour, status in ((refused, "failed"), (unreachable, "failed"), (garbled, "failed"), (slow, "timeout")):
            host = Host(behaviour, timeout_s=.05)
            result = await run_with(host, request())
            self.assertEqual(host.direct_calls[0][0], R["question"])
            self.assertEqual((result.status, result.fields["rewrite_status"]), ("ok", status))

    async def test_without_the_key_it_names_or_when_switched_off_the_text_model_is_not_called(self):
        for host, env in ((Host(R["other"]), {}), (Host(R["other"], enabled=False), ON)):
            result = await run_with(host, request(), env)
            self.assertEqual(host.text_requests, [])
            self.assertEqual(host.direct_calls[0][0], R["question"])
            self.assertEqual(result.fields["rewrite_status"], "off")

    async def test_a_relay_that_holds_the_key_is_called_without_one(self):
        host = Host(R["rewritten"], api_key_env="")
        await run_with(host, request(), {})
        self.assertEqual([key for _, key in host.text_requests], [None])

    async def test_chat_is_answered_as_before_with_its_conversation(self):
        host = Host(R["other"])
        req = request(ZH["questions"]["who"], route="chat")
        result = await run_with(host, req)
        self.assertEqual(host.text_requests, [])
        self.assertEqual(host.direct_calls, [(ZH["questions"]["who"], [], req.dialogue)])
        self.assertEqual(result.task, "offline_direct")

    async def test_without_a_fresh_frame_nothing_is_sent_anywhere(self):
        host = Host(R["other"])
        result = await run_with(host, request(current_frame=None))
        self.assertEqual((host.text_requests, host.direct_calls), ([], []))
        self.assertEqual((result.status, result.text), ("insufficient_evidence", LOCALE.message("zh", "no_usable_frames")))

    async def test_a_prepared_prompt_is_not_rewritten(self):
        host = Host(R["other"])
        question = "You are an advanced video question-answering assistant. " * 10 + "What is shown?"
        result = await run_with(host, request(question))
        self.assertEqual(host.text_requests, [])
        self.assertEqual(result.fields["rewrite_status"], "skipped")


class RecordingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = TemporaryDirectory()
        self.path = Path(self.folder.name) / "recording.mp4"
        self.path.write_bytes(b"not really a video" * 1000)

        async def frame_tokens(path, ffprobe="ffprobe"):
            return 400

        self.probe = patch.object(video_input, "frame_tokens", frame_tokens)
        self.probe.start()

    async def asyncTearDown(self):
        self.probe.stop()
        self.folder.cleanup()

    async def ask(self, host, env=ON, **changes):
        req = request(R["video"], mode="offline", route=None, **{**dict(
            current_frame=None, frames=(), recording=Recording(self.path), asked_at=20.0, dialogue=(),
            deadline=time.monotonic() + 145), **changes})
        return req, await run_with(host, req, env)

    async def test_a_whole_recording_is_watched_as_one_video_without_dialogue(self):
        host = Host(R["video_rewritten"])
        req, result = await self.ask(host, dialogue=((R["video"], ZH["answers"]["cards"]),))
        body = host.video_requests[0]
        self.assertEqual(host.video_url, MODEL_URL)
        self.assertEqual([m["role"] for m in body["messages"]], ["user"])
        video, text = body["messages"][0]["content"]
        self.assertTrue(video["video_url"]["url"].startswith("data:video/mp4;base64,"))
        self.assertEqual(text["text"], R["video_rewritten"])
        self.assertEqual(body["media_io_kwargs"]["video"]["num_frames"], 28)      # the encoder's 11552 tokens // 400
        self.assertEqual((result.text, result.task, host.direct_calls), (ZH["answers"]["cards"], "offline_video", []))
        self.assertEqual((result.fields["video_frames"], result.fields["rewrite_status"]), (28, "rewritten"))
        self.assertEqual((result.fields["backend"], result.fields["source_window_s"]), ("offline_model", [0., 20.0]))
        self.assertEqual(req.recording.sampled, [])

    async def test_a_question_before_the_end_only_sees_frames_up_to_it(self):
        host = Host(None)
        req, result = await self.ask(host, asked_at=12.0)
        self.assertEqual(host.video_requests, [])
        self.assertEqual(req.recording.sampled, [subtasks.video_plan(req.recording, host.settings.chat)])
        self.assertEqual([frame["pts"] for frame in host.direct_calls[0][1]], [0., 10.])
        self.assertEqual(result.task, "offline_rewrite")

    async def test_a_server_without_room_gets_fewer_frames(self):
        def crowded(body):
            if body["media_io_kwargs"]["video"]["num_frames"] > 7:
                return httpx.Response(400, json={"error": {"message": "exceeds the maximum context length"}})
            return httpx.Response(200, json={"choices": [{"message": {"content": ZH["answers"]["cards"]}, "finish_reason": "stop"}]})

        host = Host(None, crowded, enabled=False)
        _, result = await self.ask(host)
        self.assertEqual([b["media_io_kwargs"]["video"]["num_frames"] for b in host.video_requests], [28, 14, 7])
        self.assertEqual(result.fields["video_frames"], 7)

    async def test_an_answer_cut_at_the_length_limit_is_still_the_answer(self):
        def cut(body):
            return httpx.Response(200, json={"choices": [{"message": {"content": ZH["answers"]["cut"]},
                                                          "finish_reason": "length"}]})

        _, result = await self.ask(Host(None, cut, enabled=False))
        self.assertEqual((result.text, result.status, result.fields["finish_reason"]), (ZH["answers"]["cut"], "ok", "length"))

    async def test_when_the_video_cannot_be_answered_frames_are_sampled_instead(self):
        for broken in (lambda body: httpx.Response(500), lambda body: httpx.Response(200, json={"choices": []}),
                       lambda body: httpx.Response(200, json={"choices": [{"message": {"content": " "}}]}),
                       lambda body: httpx.Response(400, json={"error": {"message": "maximum context length"}})):
            host = Host(None, broken, enabled=False)
            req, result = await self.ask(host)
            self.assertEqual(req.recording.sampled, [subtasks.video_plan(req.recording, host.settings.chat)])
            self.assertEqual((result.text, result.task), (ZH["answers"]["cup_short"], "offline_rewrite"))

    async def test_a_chat_backend_without_video_samples_frames(self):
        host = Host(None, enabled=False)
        await host.chat.aclose()
        host.chat = Chat()
        host.chat.aclose = host.http.aclose
        req, result = await self.ask(host)
        self.assertEqual(req.recording.sampled, [subtasks.video_plan(req.recording, host.settings.chat)])
        self.assertEqual(result.task, "offline_rewrite")


class LoaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_factory_gets_the_host_if_it_takes_one(self):
        host = Host()
        try:
            self.assertIs(subtasks.load("ov25_harness.rewrite:factory", host).host, host)
            self.assertEqual(subtasks.load("tests.test_rewrite:plain_factory", host), "made without the host")
            self.assertIsInstance(subtasks.load("", host), subtasks.DirectSubtask)
        finally:
            await host.aclose()

    async def test_the_harness_loads_the_configured_subtask(self):
        harness = Harness(Chat(), settings(subtask={"factory": "ov25_harness.rewrite:factory"}), LOCALE)
        try:
            self.assertIsInstance(harness.subtask, rewrite.RewriteSubtask)
            self.assertEqual(harness.subtask_name, "ov25_harness.rewrite:factory")
            # Disabled without [rewrite] enabled: the plugin answers as DirectSubtask would.
            self.assertFalse(harness.subtask.enabled)
        finally:
            await harness.close()
        harness = Harness(Chat(), settings(), LOCALE)
        try:
            self.assertEqual((harness.subtask_name, type(harness.subtask)), ("direct", subtasks.DirectSubtask))
        finally:
            await harness.close()


def plain_factory():
    return "made without the host"


if __name__ == "__main__":
    unittest.main()
