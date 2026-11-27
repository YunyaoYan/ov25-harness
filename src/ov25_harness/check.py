"""Everything a client can do through the service, checked against what it should get back.

    ov25-harness check http://127.0.0.1:8100 tests/data/frame.jpg [--voice] [--skip-recording] [--language en|zh]
                       [--live-idle-s SECONDS]

Opens its own sessions and closes them: a live session with every route, a slow and then silent
one for the frame warnings, one with the offline subtask, a caption session, an uploaded recording
(made from the picture with ffmpeg) and, with --voice, speech at 16 and 24 kHz. The questions are
the [check] samples of the chosen locale (default: the service's first language). Checks look at
routes, tasks, statuses, fields and HTTP codes, never at the model's wording. Prints one line per
check and returns 1 if any failed. Takes about a minute against the fake backends.
"""
import argparse
import asyncio
import json
from pathlib import Path
import tempfile
import time

import httpx

from .locale import Locale, available

JPEG = {"Content-Type": "image/jpeg"}
ANSWER_KEYS = ("route", "task", "status", "text")


class Checker:
    """The service under test, the picture it is shown, and what failed so far."""

    def __init__(self, base, image, language, live_idle_s=10.):
        self.base, self.image, self.language, self.live_idle_s = base, image, language, live_idle_s
        self.questions = Locale((language,)).check(language)
        self.locale = None      # the service's active languages, once healthz has named them
        self.failed = []

    def check(self, name, ok, got=None):
        print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"  <- {str(got)[:200]}"), flush=True)
        if not ok:
            self.failed.append(name)

    def says(self, key, row):
        """Whether a row carries the service's own message for key (the harness's words, not the model's)."""
        if self.locale is None:
            return True
        question = self.questions["stop"]
        return row.get("text") == self.locale.message(self.locale.pick(question), key)


class Live:
    """A live session that gets a frame a second, as a client's camera sends them."""

    def __init__(self, run, c, period=1., **options):
        self.run, self.c, self.period, self.options = run, c, period, options
        self.cursor, self.t, self.frames = 0, 0, []

    async def __aenter__(self):
        r = await self.c.post("/v1/sessions", json={"fps": 1.0, **self.options})
        self.status, self.reply = r.status_code, r.json()
        self.root = "/v1/sessions/" + self.reply["session_id"]
        self.pusher = asyncio.create_task(self.push())
        await asyncio.sleep(3.5 * self.period)
        return self

    async def push(self):
        start = time.monotonic()
        while True:
            await asyncio.sleep(max(0., start + self.t * self.period - time.monotonic()))
            r = await self.c.post(f"{self.root}/frame", params={"t": self.t * self.period},
                                  content=self.run.image, headers=JPEG)
            self.frames.append(r.json())
            self.t += 1

    async def rows(self, wait=5):
        got = (await self.c.get(f"{self.root}/results", params={"after": self.cursor, "wait": wait})).json()["results"]
        self.cursor = max([self.cursor, *(row["seq"] for row in got)])
        return got

    async def ask(self, text, receipts=False, **body):
        """The answer row to a question; watch alerts and, unless asked for, watch receipts are skipped."""
        r = await self.c.post(f"{self.root}/text", json={"text": text, "t": max(self.t - 1, 0) * self.period, **body})
        if r.status_code >= 400:
            return dict(http=r.status_code)
        end = time.monotonic() + 30
        while time.monotonic() < end:
            for row in await self.rows():
                if (row["type"] == "answer" and row.get("status") != "model_report"
                        and (receipts or row.get("status") != "watch_accepted")):
                    return row
        return dict(status="no answer")

    async def __aexit__(self, *exc):
        self.pusher.cancel()
        await asyncio.gather(self.pusher, return_exceptions=True)
        self.closed = await self.c.delete(self.root)


def answered(row, route=None, task=None, backend=None):
    """A non-empty ok answer, from the given route, task and backend (a set allows any of them)."""
    def fits(value, wanted):
        return wanted is None or (value in wanted if isinstance(wanted, (set, frozenset)) else value == wanted)

    return (row.get("status") == "ok" and bool(row.get("text")) and fits(row.get("route"), route)
            and fits(row.get("task"), task) and fits(row.get("backend"), backend))


def brief(row):
    return {key: row.get(key) for key in (*ANSWER_KEYS, "backend", "route_decided_by", "http") if key in row}


async def live(run, c):
    q = run.questions
    async with Live(run, c, mode="qa") as s:
        check = run.check
        check("open a live session", s.status == 201 and s.reply.get("api_version") == 1
              and {"frame_url", "text_url", "results_url", "events_url", "watch_url", "logs_url"} <= set(s.reply), s.reply)
        check("a frame a second is healthy", s.frames[-1].get("accepted") and s.frames[-1]["health"]["status"] == "ok",
              s.frames[-1])
        r = await c.post(f"{s.root}/frame", params={"t": 0.}, content=run.image, headers=JPEG)
        check("a second frame for the same second is not taken", r.json().get("reason") == "live_1hz_sampling", r.text)
        r = await c.post(f"{s.root}/frame", params={"t": 999.}, content=b"not a picture", headers=JPEG)
        check("a body that is not a picture is refused", r.status_code == 415, r.status_code)

        seen = []

        async def events():
            async with c.stream("GET", f"{s.root}/events") as stream:
                async for line in stream.aiter_lines():
                    if line.startswith("data: "):
                        seen.append(json.loads(line[6:])["type"])
                        if "answer" in seen:
                            return
        listener = asyncio.create_task(events())
        row = await s.ask(q["current"], time_scope="current")
        check("a question about now: the offline model, current frame",
              answered(row, "realtime", "realtime", "offline_model") and row.get("route_decided_by") == "rule"
              and len(row.get("source_frame_indices") or ()) == 1, brief(row))
        await asyncio.wait({listener}, timeout=10)
        check("the event stream carries the answer", "answer" in seen, seen)
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)
        row = await s.ask(q["scene"])
        check("a scene question routed by the model", answered(row) and row.get("route_decided_by") == "model", brief(row))
        row = await s.ask(q["past"])
        # The stream answers, or, when it stays silent, the offline model from the session buffer.
        check("a question about the past: the stream or its fallback",
              answered(row, "past", "past", {"streaming_session", "offline_model"})
              and (row["backend"] == "streaming_session") == (row.get("coverage") == "stream")
              and len(row.get("source_window_s") or ()) == 2, brief(row))
        row = await s.ask(q["identity"])
        check("chat: the offline model, no picture",
              answered(row, "chat", "chat", "offline_model") and "source_window_s" not in row, brief(row))
        row = await s.ask(q["watch"], receipts=True)
        check("a watch is accepted", row.get("status") == "watch_accepted" and row.get("route") == "watch"
              and row.get("task") == "watch", brief(row))
        row = await s.ask(q["stop"])
        check("stopping it by words", row.get("route") == "cancel" and run.says("stopped", row), brief(row))
        row = await s.ask(q["stop"])
        check("nothing left to stop", row.get("route") == "cancel" and run.says("nothing_to_stop", row), brief(row))
        await s.ask(q["watch_object"], receipts=True)
        stops = [(await c.delete(f"{s.root}/watch")).json().get("stopped") for _ in range(2)]
        check("stopping it through the API", stops == [True, False], stops)
        check("an empty question is refused", (await s.ask("  ")).get("http") == 422)
        r = await c.post(f"{s.root}/text", json={"text": q["short"]})
        check("a live question needs its time", r.status_code == 422, r.status_code)
        r = await c.get(f"{s.root}/logs", headers={"X-Session-Log-Token": s.reply["logs_token"]})
        check("the session log opens with its token", r.status_code == 200 and "events" in r.json(), r.text[:100])
        check("and not without it", (await c.get(f"{s.root}/logs")).status_code == 404)
    check("closing reports what was done", s.closed.status_code == 200 and s.closed.json()["frames"] > 0, s.closed.text)
    check("an unknown session is 404", (await c.get("/v1/sessions/nope/results")).status_code == 404)


async def frame_health(run, c):
    check = run.check
    async with Live(run, c, period=3., mode="qa") as s:
        await asyncio.sleep(3.5)     # past the first 10 s, in which a session is not yet judged
        rows = await s.rows(wait=0)
        check("a frame every 3 s: the response says so", s.frames[-1]["health"]["status"] == "low_frame_rate",
              s.frames[-1])
        check("and a warning row follows", any(row["type"] == "warning" for row in rows), rows)
        s.pusher.cancel()
        # The service's [policy] live_idle_s, and time for the reaper and the last long poll.
        end, closed = time.monotonic() + run.live_idle_s + 10, None
        while time.monotonic() < end and closed is None:
            r = await c.get(f"{s.root}/results", params={"after": s.cursor, "wait": 5})
            if r.status_code != 200:
                break
            for row in r.json()["results"]:
                s.cursor = max(s.cursor, row["seq"])
                closed = row if row["type"] == "closed" else closed
        check("no frame for live_idle_s: the session is closed with the reason",
              closed is not None and closed.get("reason") == "no_frames" and closed.get("text"), closed)
        await asyncio.sleep(1.5)
        r = await c.post(f"{s.root}/frame", params={"t": 99.}, content=run.image, headers=JPEG)
        check("and says why afterwards", r.status_code == 404 and "no frame" in r.json().get("detail", ""), r.text)


async def modes(run, c):
    q, check = run.questions, run.check
    async with Live(run, c, mode="qa", offline_subtask=True) as s:
        row = await s.ask(q["current"], time_scope="current")
        check("offline_subtask=true: the subtask answers",
              answered(row, "offline") and str(row.get("task")).startswith("offline_"), brief(row))
        row = await s.ask(q["past"])
        check("but the past still goes to the stream or its fallback",
              answered(row, "past", "past", {"streaming_session", "offline_model"}), brief(row))
    async with Live(run, c, mode="caption") as s:
        check("open a caption session", s.status == 201 and s.reply.get("mode") == "caption", s.reply)
        check("questions still work in it", answered(await s.ask(q["current"], time_scope="current"), "realtime"))
    for option, value in (("caption_bias", 2.0), ("sampling", "uniform_rgb"), ("max_responses", 3)):
        r = await c.post("/v1/sessions", json={"mode": "qa", option: value})
        check(f"a removed open option is refused: {option}", r.status_code == 422, r.status_code)
    r = await c.post("/v1/sessions", json={"mode": "qa", "max_visual_frames": 10 ** 6})
    check("max_visual_frames past the service's limit is refused", r.status_code == 422, r.status_code)


async def recording(run, c):
    q, check = run.questions, run.check
    with tempfile.TemporaryDirectory() as folder:
        picture, clip = Path(folder) / "frame.jpg", Path(folder) / "clip.mp4"
        picture.write_bytes(run.image)
        try:
            ffmpeg = await asyncio.create_subprocess_exec(
                "ffmpeg", "-v", "error", "-y", "-loop", "1", "-i", str(picture), "-t", "6", "-r", "25",
                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip),
                stdin=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _, error = await ffmpeg.communicate()
            if ffmpeg.returncode:
                raise OSError(error.decode(errors="replace").strip()[:200])
        except OSError as error:
            return check("make a test recording with ffmpeg (or pass --skip-recording)", False, error)
        video = clip.read_bytes()
    r = await c.post("/v1/sessions", json={"input_mode": "offline", "fps": 25.0})
    check("open a recording session", r.status_code == 201 and "video_url" in r.json(), r.text)
    if r.status_code != 201:
        return
    root, cursor = "/v1/sessions/" + r.json()["session_id"], 0
    try:
        check("no question before the upload", (await c.post(f"{root}/text", json={"text": q["video"]})).status_code == 409)
        check("no frames into a recording session",
              (await c.post(f"{root}/frame", params={"t": 0.}, content=run.image, headers=JPEG)).status_code == 409)
        r = await c.post(f"{root}/video", content=video, headers={"Content-Type": "video/mp4"})
        check("upload the recording", r.status_code == 201 and r.json().get("source_frames") == 150, r.text)
        r = await c.post(f"{root}/text", json={"text": q["video"], "time_scope": "past"})
        check("time_scope is for live sessions", r.status_code == 422, r.status_code)
        check("no question past its end",
              (await c.post(f"{root}/text", json={"text": q["after_end"], "t": 9999.})).status_code == 422)
        check("no watch on a recording", (await c.delete(f"{root}/watch")).status_code == 409)
        await c.post(f"{root}/text", json={"text": q["video"]})
        row, end = None, time.monotonic() + 60
        while row is None and time.monotonic() < end:
            for got in (await c.get(f"{root}/results", params={"after": cursor, "wait": 5})).json()["results"]:
                cursor = max(cursor, got["seq"])
                row = got if got["type"] == "answer" else row
        row = row or {}
        check("a question about the recording is answered",
              answered(row, "offline") and str(row.get("task")).startswith("offline_"), brief(row))
    finally:
        await c.delete(root)


async def voice(run, c, rate):
    from websockets.asyncio.client import connect

    check = run.check
    async with Live(run, c, mode="qa", voice=True) as s:
        async with connect(run.base.replace("http", "ws", 1) + f"{s.root}/audio") as ws:
            await ws.send(json.dumps({"type": "hello", "rate": 16000, "out_rate": rate, "vad": "client", "t": 3.0}))
            ready = json.loads(await asyncio.wait_for(ws.recv(), 10))
            check(f"voice at {rate} Hz: the socket is ready", ready.get("type") == "ready" and ready.get("out_rate") == rate,
                  ready)
            await c.post(f"{s.root}/text", json={"text": run.questions["identity"], "t": float(s.t - 1)})
            seen, received, end = [], 0, None
            try:
                while end is None and "error" not in seen:
                    message = await asyncio.wait_for(ws.recv(), 30)
                    if isinstance(message, bytes):
                        received += len(message)
                    else:
                        message = json.loads(message)
                        seen.append(message.get("type"))
                        end = message if message.get("type") == "audio.end" else None
            except TimeoutError:
                seen.append("timeout")
            check(f"voice at {rate} Hz: the answer is spoken",
                  end is not None and received > rate and end["samples"] * 2 == received, (seen, received, end))
            await ws.send(json.dumps({"type": "bye"}))


async def run_checks(base, image, language, *, voice_rates=(), recorded=True, live_idle_s=10.):
    async with httpx.AsyncClient(base_url=base, timeout=60, trust_env=False) as c:
        r = await c.get("/healthz")
        health = r.json() if r.status_code == 200 else {}
        languages = health.get("languages") or ["en"]
        run = Checker(base, image, language or languages[0], live_idle_s)
        try:
            run.locale = Locale(tuple(languages))
        except (OSError, ValueError):
            run.locale = None      # a language this client has no locale for: message texts go unchecked
        run.check("healthz", r.status_code == 200 and health.get("ok") is True and health.get("api_version") == 1
                  and r.headers.get("x-ov25-harness-version") == health.get("version")
                  and {"sessions", "voice", "streaming", "chat", "subtask"} <= set(health), health)
        run.check(f"the service has {run.language} active", run.language in languages, languages)
        print(f"     version {health.get('version')}, streaming {health.get('streaming')}, chat {health.get('chat')},"
              f" subtask {health.get('subtask')}, sessions {health.get('sessions')}, voice {health.get('voice')}")
        await live(run, c)
        await frame_health(run, c)
        await modes(run, c)
        if recorded:
            await recording(run, c)
        for rate in voice_rates:
            await voice(run, c, rate)
        left = (await c.get("/healthz")).json().get("sessions")
        print(f"     sessions open afterwards: {left}")
    print(f"\n{'ALL OK' if not run.failed else 'FAILED: ' + '; '.join(run.failed)}")
    return run.failed


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ov25-harness check", description=__doc__.split("\n\n")[0])
    parser.add_argument("url", help="the service's base URL, e.g. http://127.0.0.1:8100")
    parser.add_argument("image", type=Path, help="a JPEG or PNG picture to send as the camera's frames")
    parser.add_argument("--voice", action="store_true", help="also check speech at 16 and 24 kHz")
    parser.add_argument("--skip-recording", action="store_true",
                        help="skip the recording checks (they need ffmpeg and ffprobe on both sides)")
    parser.add_argument("--language", choices=available(), help="the locale whose sample questions are asked")
    parser.add_argument("--live-idle-s", type=float, default=10., metavar="SECONDS",
                        help="the service's [policy] live_idle_s, which the idle-close check waits out (default 10)")
    args = parser.parse_args(argv)
    failed = asyncio.run(run_checks(args.url.rstrip("/"), args.image.read_bytes(), args.language,
                                    voice_rates=(16000, 24000) if args.voice else (),
                                    recorded=not args.skip_recording, live_idle_s=args.live_idle_s))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
