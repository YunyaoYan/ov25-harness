"""One client session on the app side: its result queue and subscribers, the reader of its
streaming source's events, and sending frames to that source.

Timing: the client sends each frame's own capture time in seconds since the session opened. The
model reads that time, so it has to be the real elapsed time, not a counter. frame_index/fps carry
the same time into the vision positions; caption training advances the frame index by 4 per
second, which is what the session's fps selects.
"""
from __future__ import annotations

import asyncio
from collections import deque
import logging
import math
from pathlib import Path
import shutil
import time
from typing import Any
import uuid

from .frame_health import FrameHealth
from .session_log import SessionLog
from .stream import drop_user_stream

_LOG = logging.getLogger(__name__)
SOURCE_CLOSE_S = 5.
SYSTEM_PROMPT = "You are a real-time streaming video assistant."
# Engine events a session opened with native_events gets as rows, and the fields they carry.
NATIVE_EVENTS = {"input.done", "response.created", "response.done", "context.compacted"}
NATIVE_FIELDS = ("input_id", "response_id", "status", "control", "reason", "retained_turns",
                 "retained_visual_frames")


class SourceFailed(RuntimeError):
    """A frame could not be sent and the source could not be dropped either."""


class SessionRuntime:
    def __init__(self, source: Any, fps: float, *, locale, offline_dir: Path | None = None,
                 native_events=False) -> None:
        # The engine session (None for an offline session or once dropped) and the harness side.
        self.source = source
        self.session = None
        self.locale = locale
        self.native_events = native_events
        self.fps = fps
        self.results: deque[dict] = deque(maxlen=512)
        self.controls: deque[dict] = deque(maxlen=4096)
        self.cursor = 0
        self.opened = time.time()
        # Frames seen, and the first frame's declared and arrival times, so
        # the log can show the declared rate next to the real one.
        self.frames = 0
        self.first_frame: tuple[float, float] | None = None
        self.failure: str | None = None
        self.voice = False
        self.offline_dir = offline_dir
        self.upload_lock = asyncio.Lock()
        self.frame_lock = asyncio.Lock()
        self.last_live_second = -1
        self.last_live_wall = -math.inf
        self.last_activity = time.monotonic()
        self.frame_health = FrameHealth(say=self.say)
        self.closing = False
        self.audio_task = None
        self.upload_task = None
        self.diagnostics: SessionLog | None = None
        # One audio socket per session; a second one is refused rather than
        # left to fight the first over the same speaker.
        self.audio_open = False
        self._second_of_input: dict[str, float] = {}
        self._second_of_response: dict[str, float] = {}
        self._waiters: list[asyncio.Queue] = []
        self.reader = asyncio.create_task(self._read()) if source is not None else None

    def say(self, key, **fields):
        """A message in the language of the session's last question."""
        language = self.session.language if self.session is not None else self.locale.default
        return self.locale.message(language, key, **fields)

    async def close(self):
        self.closing = True
        source, self.source = self.source, None
        for queue in self._waiters:
            queue.put_nowait(None)
        workers = [task for task in (self.reader, self.audio_task, self.upload_task) if task is not None]
        for task in workers:
            if not task.cancelling():
                task.cancel()
        try:
            await self.session.close()
        finally:
            try:
                async with asyncio.timeout(5.):
                    await asyncio.gather(*workers, return_exceptions=True)
            finally:
                if source is not None:
                    # The client is gone. Cancel its engine work instead of waiting for it to drain.
                    async with asyncio.timeout(SOURCE_CLOSE_S):
                        await source.abort()
                if self.offline_dir is not None:
                    if self.upload_lock.locked():
                        raise RuntimeError("upload still running")
                    shutil.rmtree(self.offline_dir)

    def note(self, input_id: str, t: float) -> None:
        self._second_of_input[input_id] = t

    def emit(self, row: dict) -> None:
        self.cursor += 1
        row["seq"] = self.cursor
        self.results.append(row)
        if self.diagnostics is not None:
            self.diagnostics.output(asyncio.current_task(), row)
        # `t` is the timestamp of the input this was attached to, which is not always when the
        # text was produced -- log the frame count too, so a stale answer is recognisable.
        if row["type"] != "native_event":
            _LOG.info("answer seq=%d t=%s frame=%d %r", self.cursor, row.get("t"), self.frames,
                      str(row.get("text"))[:160])
        for queue in self._waiters:
            queue.put_nowait(row)

    async def _read(self) -> None:
        try:
            async for event in self.source.events():
                if self.session.stream is not None:
                    self.session.stream.observe(event)
                if self.native_events and event.type in NATIVE_EVENTS:
                    data = event.to_dict()
                    self.emit(dict(type="native_event", event=event.type, source="observer",
                                   t=event.timestamp_sec, **{key: data[key] for key in NATIVE_FIELDS if key in data}))
                if event.type == "input.done":
                    self.controls.append(
                        {"t": self._second_of_input.pop(event.input_id, event.timestamp_sec), "control": event.control}
                    )
                elif event.type == "response.created":
                    self._second_of_response[event.response_id] = self._second_of_input.get(
                        event.input_id, event.timestamp_sec
                    )
                if event.type == "response.output_text.done" and event.text:
                    source_t = self._second_of_response.pop(event.response_id, event.timestamp_sec)
                    # Answers are published by the harness; the source's own text only reaches the
                    # client as a caption.
                    if event.control != "caption":
                        continue
                    self.emit({"type": "caption", "t": source_t, "text": event.text,
                               "response_id": event.response_id})
                elif event.type == "response.done":
                    self._second_of_response.pop(event.response_id, None)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if drop_user_stream(self, error) or self.source is None:
                return
            self.failure = repr(error)
            self.emit({"type": "error", "text": repr(error)})

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._waiters.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        if queue in self._waiters:
            self._waiters.remove(queue)

    async def send_frame(self, data: bytes, t: float, backlog: int) -> dict:
        """One accepted frame to the source and the session buffer. Call under frame_lock."""
        first = self.frames == 0 and not self.controls and not self._second_of_input
        content: list[dict] = [
            {
                "type": "image_url",
                "image_url": {"url": data},
                "timestamp_sec": t,
                "frame_index": math.floor(t * self.fps + 1e-8),
                "fps": self.fps,
            }
        ]
        messages: list[dict] = []
        if first:
            # The system turn rides on the first frame, where the engine expects it.
            messages.append({"role": "system", "content": SYSTEM_PROMPT})
        messages.append({"role": "user", "content": content})
        if len(self._second_of_input) >= backlog:
            drop_user_stream(self, "backlog")
        if self.source is not None:
            if len(self._second_of_input) >= backlog:
                return {"accepted": False, "reason": "caption_backlog", "t": t, "cursor": self.cursor}
            input_id = uuid.uuid4().hex
            self.note(input_id, t)
            if self.session.stream is not None:
                self.session.stream.sent(input_id, t)
            try:
                async with asyncio.timeout(3.):
                    await self.source.send_messages(messages=messages, input_id=input_id)
            except (Exception, asyncio.CancelledError) as error:
                self._second_of_input.pop(input_id, None)
                if isinstance(error, asyncio.CancelledError):
                    raise
                if not drop_user_stream(self, error):
                    raise SourceFailed(repr(error)) from error
        self.session.frame(data, t, content[0]["frame_index"])
        self.frames += 1
        now = time.monotonic()
        if self.first_frame is None:
            self.first_frame = (t, now)
            _LOG.info("frame 1 at t=%.3fs, fps=%s declared at open", t, self.fps)
        elif self.frames % 25 == 0:
            t0, wall0 = self.first_frame
            said, real = t - t0, now - wall0
            _LOG.info("frame %d: %.2f fps by the client's own clock, %.2f fps as received "
                      "(%.1fs declared vs %.1fs real)", self.frames, self.frames / said if said else math.inf,
                      self.frames / real if real else math.inf, said, real)
        return {"accepted": True, "t": t, "bytes": len(data), "cursor": self.cursor}
