"""The HTTP front end: sessions a device or a script can drive with plain calls.

The streaming engine's own API is fine for a Python caller but awkward for a device: it wants
base64 data URLs inside JSON and an SSE reader. This keeps the session on the server side and gives
the client a few plain calls - open, post a JPEG, ask, read results - with a polling route for
clients that cannot hold an SSE connection open.

    POST   /v1/sessions                  open a session
    POST   /v1/sessions/{sid}/frame      raw JPEG/PNG body, ?t=<seconds>
    POST   /v1/sessions/{sid}/video      raw MP4/MOV body for offline sessions
    POST   /v1/sessions/{sid}/text       {"text": ..., "t": ..., "time_scope": ...}
    GET    /v1/sessions/{sid}/results    ?after=<cursor>&wait=<seconds>  (polling)
    GET    /v1/sessions/{sid}/events     server-sent events
    WS     /v1/sessions/{sid}/audio      microphone PCM in, speech out
    DELETE /v1/sessions/{sid}/watch      stop the running watch
    GET    /v1/sessions/{sid}/logs       the session's diagnostics (X-Session-Log-Token)
    DELETE /v1/sessions/{sid}            close
    GET    /healthz

See docs/api.md and docs/voice-protocol.md.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
from pathlib import Path
from tempfile import mkdtemp
import time
from typing import Literal
import uuid

from fastapi import Body, FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.websockets import WebSocketDisconnect

from . import API_VERSION, __version__
from .audio import run_audio
from .config import MAX_COMPACT_TO_VISUAL_FRAMES
from .harness import MAX_T_S, Harness, LiveSession, LiveStream
from .locale import Locale
from .recording import InvalidRecording, Recording
from .runtime import SessionRuntime, SourceFailed
from .session_log import SessionLog
from .stream import StreamPool, open_user_stream

_LOG = logging.getLogger(__name__)
MIN_FRAME_WALL_GAP_S = .5
# The full 8-byte PNG signature; JPEG has no longer fixed one.
MEDIA_TYPES = {b"\xff\xd8\xff": "jpeg", b"\x89PNG\r\n\x1a\n": "png"}
VERSION_HEADER = b"x-ov25-harness-version"
# frame_index = floor(t * fps) reaches the engine as a signed 64-bit integer: it must stay below this.
FRAME_INDEX_LIMIT = 2 ** 63


def stream_audit(kind: str, **data) -> None:
    """The user stream's open/drop/close notes, in the log."""
    _LOG.info("%s %s", kind, " ".join(f"{key}={value}" for key, value in data.items()))


class OpenBody(BaseModel):
    # Options the old API took and this one does not (caption_bias, sampling, sample_budget,
    # max_responses) are refused rather than silently ignored.
    model_config = ConfigDict(extra="forbid")

    input_mode: Literal["live", "offline"] = "live"
    # "caption" turns on <caption_begin> and <captioning>; questions still work.
    mode: Literal["caption", "qa"] | None = None
    # The camera's own frame rate. frame_index/fps carries wall-clock time into
    # the vision positions, and training writes the source video's rate, so the
    # client should send its own rather than take this default.
    fps: float = Field(default=30.0, gt=0)
    native_events: bool = False
    # Hold answers back from memory until the audio socket reports what the
    # user actually heard. Without it an interrupted answer is remembered
    # whole, and the model goes on as if it had said all of it.
    voice: bool = False
    max_visual_frames: int | None = Field(default=None, ge=1)
    compact_to_visual_frames: int | None = Field(default=None, ge=1)
    # Real-time questions of a live session go straight to the offline model. With this set they
    # go to the offline subtask instead, which may rewrite and plan them. Offline recordings always do.
    offline_subtask: bool = False


class TextBody(BaseModel):
    text: str = Field(min_length=1)
    t: float | None = Field(default=None, ge=0, le=MAX_T_S)
    # Who decides whether a live question is about the current frame or the past.
    time_scope: Literal["auto", "current", "past"] = "auto"


class VersionHeader:
    """The harness version on every HTTP response."""

    def __init__(self, app, version: str):
        self.app, self.header = app, (VERSION_HEADER, version.encode("ascii"))

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def tagged(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", ()), self.header]}
            await send(message)

        await self.app(scope, receive, tagged)


def build_app(settings, *, chat, streaming, transcriber=None, speaker=None, subtask=None) -> FastAPI:
    """The app. chat: async complete(body, timeout) -> (text, finish_reason); streaming:
    session(**options) -> an engine session; subtask: None for [subtask] factory, a spec string,
    a factory, or a subtask object. The app closes chat and the voice backends when it stops."""
    server, policy, media, streaming_settings = settings.server, settings.policy, settings.media, settings.streaming
    locale = Locale(server.languages)
    harness = Harness(chat, settings, locale, subtask)
    pool = StreamPool(streaming_settings.user_streams)
    voice = transcriber is not None and speaker is not None
    sessions: dict[str, SessionRuntime] = {}
    closing: dict[str, asyncio.Task] = {}
    # Why the app itself closed a session, for the client that comes back to it.
    ended: dict[str, str] = {}
    offline_uploads = asyncio.Semaphore(1)

    def begin_close(sid):
        if sid in closing:
            return closing[sid]
        runtime = sessions.get(sid)
        if runtime is None:
            return None
        runtime.closing = True

        async def cleanup():
            try:
                await runtime.close()
                return True
            except Exception as error:
                _LOG.warning("session cleanup unconfirmed: %s", type(error).__name__)
                return False
            finally:
                sessions.pop(sid, None)
                closing.pop(sid, None)

        task = closing[sid] = asyncio.create_task(cleanup())
        return task

    async def reap_idle():
        while True:
            await asyncio.sleep(min(1., policy.live_idle_s, policy.offline_idle_s))
            now = time.monotonic()
            for sid, runtime in list(sessions.items()):
                if runtime.closing:
                    continue
                live = runtime.offline_dir is None
                if now - runtime.last_activity < (policy.live_idle_s if live else policy.offline_idle_s):
                    if live and (row := runtime.frame_health.warning(now)) is not None:
                        runtime.emit(row)
                else:
                    if live:
                        # Tell whoever is still listening, and whoever asks for this session later.
                        text = runtime.say("closed_idle", seconds=f"{policy.live_idle_s:g}")
                        runtime.emit(dict(type="closed", t=runtime.frame_health.last_t, reason="no_frames", text=text))
                        ended[sid] = f"session closed: no frame for {policy.live_idle_s:g} s"
                        while len(ended) > 1024:
                            del ended[next(iter(ended))]
                    begin_close(sid)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        reaper = asyncio.create_task(reap_idle())
        try:
            yield
        finally:
            reaper.cancel()
            await asyncio.gather(reaper, return_exceptions=True)
            await asyncio.gather(*(begin_close(sid) for sid in list(sessions)))
            await harness.close()
            for backend in (chat, transcriber, speaker):
                if (aclose := getattr(backend, "aclose", None)) is not None:
                    with contextlib.suppress(Exception):
                        await aclose()

    app = FastAPI(title="ov25-harness", version=__version__, lifespan=lifespan)
    app.add_middleware(VersionHeader, version=__version__)
    app.state.settings, app.state.harness, app.state.pool, app.state.sessions = settings, harness, pool, sessions

    def gone(sid: str) -> str:
        """Why a session is not there: the app's own reason when it closed the session itself."""
        return ended.get(sid, "unknown session")

    def get(sid: str) -> SessionRuntime:
        runtime = sessions.get(sid)
        if runtime is None:
            raise HTTPException(status_code=404, detail=gone(sid))
        if runtime.closing:
            raise HTTPException(status_code=409, detail=ended.get(sid, "session is closing"))
        if runtime.failure:
            raise HTTPException(status_code=409, detail=runtime.failure)
        return runtime

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True, "version": __version__, "api_version": API_VERSION,
                "sessions": len(sessions), "closing_sessions": len(closing), "voice": voice,
                "languages": list(locale.languages), "streaming": streaming_settings.kind,
                "chat": settings.chat.kind, "subtask": harness.subtask_name}

    @app.post("/v1/sessions", status_code=201)
    async def open_session(response: Response, body: OpenBody = Body(default=OpenBody())) -> dict:
        if body.input_mode == "offline":
            if sum(runtime.offline_dir is not None for runtime in sessions.values()) >= media.max_offline_sessions:
                raise HTTPException(status_code=503, detail="offline session limit reached")
            sid = uuid.uuid4().hex
            directory = Path(mkdtemp(prefix="ov25-recording-"))
            runtime = SessionRuntime(None, body.fps, locale=locale, offline_dir=directory)
            runtime.voice = body.voice
            runtime.session = LiveSession(harness, runtime.emit, body.fps, server.max_pixels, body.voice,
                                          input_mode="offline")
            sessions[sid] = runtime
            url = f"/v1/sessions/{sid}"
            reply = dict(session_id=sid, api_version=API_VERSION, input_mode="offline",
                         video_url=url + "/video", text_url=url + "/text",
                         results_url=url + "/results", events_url=url + "/events")
            if body.voice:
                reply["audio_url"] = url + "/audio"
            return reply
        mode = body.mode or "qa"
        enable = ["caption"] if mode == "caption" else []
        # Unbounded is not a safe default: see [streaming] max_visual_frames.
        max_frames = body.max_visual_frames
        compact_to = body.compact_to_visual_frames
        if max_frames is None:
            max_frames = streaming_settings.max_visual_frames
            compact_to = compact_to or streaming_settings.compact_to_visual_frames
        if compact_to is None:
            # Left at the limit, the source would be rebuilt on every frame past it.
            compact_to = max(1, min(max_frames // 2, MAX_COMPACT_TO_VISUAL_FRAMES))
        limit = streaming_settings.max_visual_frames_limit
        if not (1 <= compact_to <= min(max_frames, MAX_COMPACT_TO_VISUAL_FRAMES) and max_frames <= limit):
            raise HTTPException(status_code=422, detail=f"need max_visual_frames <= {limit} and"
                                f" 1 <= compact_to_visual_frames <= min(max_visual_frames, {MAX_COMPACT_TO_VISUAL_FRAMES})")
        # Every live session has its own streaming source: frames go to it, and past questions,
        # watches and captions come from it.
        source = await open_user_stream(pool, streaming, audit=stream_audit, enable=enable, max_responses=1,
                                        max_visual_frames=max_frames, compact_to_visual_frames=compact_to,
                                        answer_commit="client")
        if source is None:
            raise HTTPException(status_code=503, detail="no streaming capacity for a live session")
        sid = uuid.uuid4().hex
        runtime = SessionRuntime(source, body.fps, locale=locale, native_events=body.native_events)
        asked = "client" if body.max_visual_frames is not None else "default"
        _LOG.info("session %s mode=%s fps=%s voice=%s frames=%s/%s (%s)", sid[:8], mode, body.fps, body.voice,
                  max_frames, compact_to, asked)
        runtime.voice = body.voice
        runtime.session = LiveSession(harness, runtime.emit, body.fps, server.max_pixels, body.voice,
                                      offline_subtask=body.offline_subtask)
        runtime.session.stream = LiveStream(runtime, stream_audit, pool)
        sessions[sid] = runtime
        url = f"/v1/sessions/{sid}"
        reply = {"session_id": sid, "api_version": API_VERSION, "mode": mode, "fps": body.fps,
                 "input_mode": "live",
                 "max_visual_frames": max_frames, "compact_to_visual_frames": compact_to, "frame_interval_s": 1,
                 "native_events": body.native_events, "voice": body.voice,
                 "frame_url": url + "/frame", "text_url": url + "/text",
                 "results_url": url + "/results", "events_url": url + "/events"}
        if mode == "qa":
            runtime.diagnostics = SessionLog()
            runtime.diagnostics.add("opened", mode=mode, voice=body.voice, fps=body.fps)
            reply.update(logs_url=url + "/logs", logs_token=runtime.diagnostics.token)
            response.headers["Cache-Control"] = "no-store"
        reply["watch_url"] = url + "/watch"
        if body.voice:
            reply["audio_url"] = url + "/audio"
        return reply

    @app.get("/v1/sessions/{sid}/logs")
    async def session_logs(sid: str, request: Request, after: int = Query(0, ge=0),
                           limit: int = Query(100, ge=1, le=250)):
        runtime = sessions.get(sid)
        journal = runtime.diagnostics if runtime is not None and not runtime.closing else None
        headers = {"Cache-Control": "no-store"}
        if journal is None or not journal.permits(request.headers.get("X-Session-Log-Token", "")):
            return JSONResponse({"detail": "session log unavailable"}, status_code=404, headers=headers)
        return JSONResponse({"session_id": sid, **journal.page(after, limit)}, headers=headers)

    def record_rejection(request, status):
        runtime = sessions.get(request.path_params.get("sid"))
        operation = request.url.path.rsplit("/", 1)[-1]
        if runtime is not None and runtime.diagnostics is not None and operation in {"frame", "text", "results"}:
            runtime.diagnostics.add("request_error", operation=operation, http_status=status)

    @app.exception_handler(HTTPException)
    async def rejected_request(request: Request, exc: HTTPException):
        record_rejection(request, exc.status_code)
        return await http_exception_handler(request, exc)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        record_rejection(request, 422)
        return await request_validation_exception_handler(request, exc)

    @app.exception_handler(Exception)
    async def failed_request(request: Request, exc: Exception):
        # Starlette answers an unhandled error outside every middleware, VersionHeader included;
        # the server still logs the error itself.
        return PlainTextResponse("Internal Server Error", status_code=500,
                                 headers={VERSION_HEADER.decode("ascii"): __version__})

    @app.post("/v1/sessions/{sid}/frame")
    async def post_frame(sid: str, request: Request, t: float = Query(ge=0, le=MAX_T_S)) -> dict:
        runtime = get(sid)
        if runtime.offline_dir is not None:
            raise HTTPException(status_code=409, detail="upload offline recordings to /video")
        chunks = bytearray()
        try:
            async with asyncio.timeout(3.):
                async for chunk in request.stream():
                    if len(chunks) + len(chunk) > policy.max_frame_bytes:
                        raise HTTPException(status_code=413, detail="frame exceeds the size limit")
                    chunks.extend(chunk)
        except TimeoutError as error:
            raise HTTPException(status_code=408, detail="frame upload timed out") from error
        data = bytes(chunks)
        if not any(data.startswith(magic) for magic in MEDIA_TYPES):
            raise HTTPException(status_code=415, detail="body must be a JPEG or PNG image")
        # Any positive fps is taken, so t * fps can still overflow frame_index (inf fails this too).
        if not math.isfinite(t) or not t * runtime.fps < FRAME_INDEX_LIMIT:
            raise HTTPException(status_code=422, detail="frame timestamp out of range for the session's fps")
        async with runtime.frame_lock:
            if sessions.get(sid) is not runtime or runtime.closing:
                raise HTTPException(status_code=404, detail=gone(sid))
            second = math.floor(t)
            now = time.monotonic()
            if second <= runtime.last_live_second:
                reply = {"accepted": False, "reason": "live_1hz_sampling", "t": t, "cursor": runtime.cursor}
            elif now - runtime.last_live_wall < MIN_FRAME_WALL_GAP_S:
                reply = {"accepted": False, "reason": "live_wall_rate", "t": t, "cursor": runtime.cursor}
            else:
                try:
                    reply = await runtime.send_frame(data, t, streaming_settings.frame_backlog)
                except SourceFailed as error:
                    raise HTTPException(status_code=409, detail=str(error)) from error
            if reply["accepted"]:
                runtime.last_live_second = second
                runtime.last_live_wall = runtime.last_activity = now
            # How this session's frames are doing, with every frame it sends.
            health = runtime.frame_health
            if reply["accepted"]:
                health.frame(t, now)
            reply["health"] = health.summary(now)
            if (row := health.warning(now)) is not None:
                runtime.emit(row)
            if runtime.diagnostics is not None:
                runtime.diagnostics.add("frame", t=t, bytes=len(data), accepted=reply["accepted"],
                                        reason=reply.get("reason"))
            return reply

    @app.post("/v1/sessions/{sid}/video", status_code=201)
    async def post_video(sid: str, request: Request) -> dict:
        runtime = get(sid)
        if runtime.offline_dir is None:
            raise HTTPException(status_code=409, detail="open an offline session to upload a recording")
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type not in {"video/mp4", "video/quicktime", "application/octet-stream"}:
            raise HTTPException(status_code=415, detail="send a raw MP4/MOV body")
        if runtime.session.recording is not None or runtime.upload_lock.locked():
            raise HTTPException(status_code=409, detail="this session already has a recording or upload")
        if offline_uploads.locked():
            raise HTTPException(status_code=503, detail="another recording is being uploaded")
        async with runtime.upload_lock, offline_uploads:
            if sessions.get(sid) is not runtime or runtime.closing:
                raise HTTPException(status_code=404, detail=gone(sid))
            runtime.upload_task = asyncio.current_task()
            path = runtime.offline_dir / "recording.mp4"
            try:
                size = 0
                async with asyncio.timeout(200.):
                    with path.open("wb") as handle:
                        async for chunk in request.stream():
                            size += len(chunk)
                            if size > media.max_upload_bytes:
                                raise HTTPException(status_code=413, detail="recording exceeds the upload size limit")
                            handle.write(chunk)
                    if not size:
                        raise HTTPException(status_code=422, detail="recording is empty")
                    recording = await Recording.inspect(path, ffprobe=media.ffprobe, ffmpeg=media.ffmpeg,
                                                        max_duration_s=media.max_recording_s)
                    if sessions.get(sid) is not runtime or runtime.closing:
                        raise HTTPException(status_code=404, detail=gone(sid))
                    runtime.session.recording = recording
                    runtime.last_activity = time.monotonic()
            except InvalidRecording as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
            except FileNotFoundError as error:
                raise HTTPException(status_code=503, detail="video decoder is unavailable") from error
            except TimeoutError as error:
                raise HTTPException(status_code=504, detail="recording inspection timed out") from error
            finally:
                runtime.upload_task = None
                if runtime.session.recording is None:
                    path.unlink(missing_ok=True)
            return dict(input_mode="offline", bytes=size, source_fps=recording.fps,
                        source_frames=len(recording.timestamps), last_frame_pts_s=recording.end)

    @app.post("/v1/sessions/{sid}/text")
    async def post_text(sid: str, body: TextBody) -> dict:
        runtime = get(sid)
        if runtime.offline_dir is not None and runtime.session.recording is None:
            raise HTTPException(status_code=409, detail="upload the recording first")
        if runtime.offline_dir is None and body.t is None:
            raise HTTPException(status_code=422, detail="live questions require t")
        try:
            if body.time_scope != "auto" and runtime.offline_dir is not None:
                raise ValueError("time_scope applies to live sessions")
            runtime.session.submit(body.text, body.t,
                                   time_scope=None if body.time_scope == "auto" else body.time_scope)
            if runtime.diagnostics is not None:
                runtime.diagnostics.question(runtime.session.request, body.t, body.text, "text")
            if runtime.offline_dir is not None:
                runtime.last_activity = time.monotonic()
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {"accepted": True, "t": body.t}

    @app.get("/v1/sessions/{sid}/results")
    async def results(sid: str, after: int = 0, wait: float = Query(default=0.0, ge=0, le=60)) -> dict:
        runtime = get(sid)
        rows = [row for row in runtime.results if row["seq"] > after]
        if not rows and wait:
            queue = runtime.subscribe()
            try:
                row = await asyncio.wait_for(queue.get(), timeout=wait)
                # Everything after the cursor, not only the row that woke us: a watch alert and an
                # answer can land together.
                rows = [] if row is None else [row for row in runtime.results if row["seq"] > after]
            except TimeoutError:
                rows = []
            finally:
                runtime.unsubscribe(queue)
        if runtime.diagnostics is not None:
            runtime.diagnostics.add("poll", after=after, returned_seq=[row["seq"] for row in rows])
        return {"cursor": runtime.cursor, "results": rows,
                "controls_seen": len(runtime.controls)}

    @app.get("/v1/sessions/{sid}/events")
    async def events(sid: str) -> StreamingResponse:
        runtime = get(sid)

        async def stream():
            queue = runtime.subscribe()
            try:
                for row in list(runtime.results):
                    yield f"data: {json.dumps(row, ensure_ascii=False)}\n\n"
                # The body starts after get() returned: a close that began in between may already
                # have told its subscribers, and would never tell this one.
                if runtime.closing:
                    return
                while True:
                    row = await queue.get()
                    if row is None:
                        break
                    yield f"data: {json.dumps(row, ensure_ascii=False)}\n\n"
            finally:
                runtime.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    @app.websocket("/v1/sessions/{sid}/audio")
    async def audio(ws: WebSocket, sid: str) -> None:
        await ws.accept()
        runtime = sessions.get(sid)
        if runtime is None or runtime.closing:
            await ws.close(code=4404, reason=gone(sid))
            return
        if not runtime.voice:
            await ws.close(code=4400, reason="open the session with voice: true")
            return
        if runtime.offline_dir is not None and runtime.session.recording is None:
            await ws.close(code=4409, reason="upload the recording first")
            return
        if not voice:
            await ws.close(code=4500, reason="this service has no voice backends")
            return
        if runtime.audio_open:
            await ws.close(code=4409, reason="this session already has audio")
            return
        runtime.audio_open = True
        runtime.audio_task = asyncio.current_task()
        try:
            await run_audio(ws, runtime, transcriber, speaker, sid)
        except WebSocketDisconnect:
            pass
        finally:
            runtime.audio_open = False
            runtime.audio_task = None

    @app.delete("/v1/sessions/{sid}/watch")
    async def stop_watch(sid: str) -> dict:
        runtime = get(sid)
        if runtime.offline_dir is not None:
            raise HTTPException(status_code=409, detail="session cannot run a watch")
        return {"stopped": await runtime.session.stop_watch()}

    @app.delete("/v1/sessions/{sid}")
    async def close_session(sid: str) -> dict:
        runtime = sessions.get(sid)
        if runtime is None:
            raise HTTPException(status_code=404, detail=gone(sid))
        if not await asyncio.shield(begin_close(sid)):
            raise HTTPException(status_code=503, detail="session cleanup unconfirmed")
        return {"session_id": sid, "closed": True, "results": len(runtime.results), "frames": runtime.frames}

    return app
