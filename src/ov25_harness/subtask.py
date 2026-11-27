"""The boundary between the live harness and the offline subtask.

The harness only judges and routes a question. A question that goes to the offline subtask is
handed over unchanged; rewriting it, planning a task for it, choosing frames and calling the
offline model are the subtask's own business. The subtask answers once and never watches a
stream: anything continuous stays on the harness side.

A question goes here when the session was opened with ``input_mode="offline"`` (a recording, no
routing at all) or with ``offline_subtask=true`` (a live session's real-time questions).

An implementation is any object with ``async run(request) -> OfflineResult``. Set
``[subtask] factory = "package.module:factory"`` to load one. The factory is called with the host
(see SubtaskHost), or with nothing if it takes no argument. Without it, ``DirectSubtask`` answers by
sending the question and its frames straight to the offline model; ``ov25_harness.rewrite:factory``
puts a text model's rewrite of the question in front of that. See docs/plugins.md.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import importlib
import inspect
import math
from typing import Any, Protocol


@dataclass(frozen=True)
class OfflineRequest:
    question: str              # the user's words, unchanged
    asked_at: float            # session seconds the question refers to
    input_mode: str            # "live" or "offline"
    frames: tuple              # live: frames received up to asked_at, oldest first
                               #   (dicts with png, pts, frame_index, fps)
    current_frame: dict | None  # live: the newest frame if it is fresh enough to be "now"
    recording: Any             # offline: the uploaded Recording (seconds(), indices(), rgb(), end, fps, size, path)
    max_pixels: int            # pixel budget for frames taken from a recording
    dialogue: tuple            # (question, answer) pairs the user has received, oldest first
    route: str | None          # the harness's judgment ("realtime" or "chat"); advisory only
    deadline: float            # time.monotonic() by which the answer must be back


@dataclass
class OfflineResult:
    text: str
    status: str = "ok"         # ok, insufficient_evidence, clarification, ...
    task: str = "offline_subtask"
    fields: dict = field(default_factory=dict)   # extra result-row fields, e.g. source_window_s


class OfflineSubtask(Protocol):
    async def run(self, request: OfflineRequest) -> OfflineResult: ...


class SubtaskHost(Protocol):
    """What a subtask factory is given: the harness."""

    chat: Any        # the offline model: async complete(body, timeout) -> (text, finish_reason)
    settings: Any    # ov25_harness.config.Settings
    http: Any        # an httpx.AsyncClient for other services, closed with the app
    locale: Any      # ov25_harness.locale.Locale: pick(question), message(language, key)

    async def direct(self, question: str, frames: list, dialogue: tuple, deadline: float) -> dict:
        """The question and its frames to the offline model as they are: a result-row dict."""

    def say(self, language: str, key: str, **fields) -> str:
        """A user-facing message of the locale."""


# A recording goes to the offline model the way it was trained to watch videos: frames at a steady
# rate, each behind its "<T.T seconds>" tag, so an answer can say when. The client sends no sampling
# settings; how many frames and how large follow from the recording's length and its own size, as
# benchmarks of OV2.5-4B found best (docs/design.md, "Reading a recording"):
#   - up to SHORT_SECONDS: a frame a second at about 480x270. On TimeLens and VideoMME short, two frames
#     a second and 230k or 321k pixels a frame scored no better, and took two to four times as long.
#   - longer: at least LONG_FRAMES frames, at most a frame every two seconds, about 200k pixels each,
#     as the long-video benchmarks read them. On VideoMME medium this matched a frame a second in under
#     half the time; on LongVideoBench (15-30 minutes) it was ahead of a frame a second at 480x270.
# Never larger than the recording itself, and never more than the server's context ([chat]
# context_tokens) or image limit ([chat] max_images) allows.
SHORT_SECONDS = 256
SHORT_RATE, SHORT_PIXELS = 1., 129_600
LONG_FRAMES, LONG_RATE, LONG_PIXELS = 256, .5, 200_000
# At most one visual token per 48x48 pixels (16-pixel patches merged 3x3); a frame's tag and its vision
# markers measured 9-10.5 tokens more.
TOKEN_PIXELS, TAG_TOKENS = 48, 12
# Left over for the system turn, the question and the answer.
RESERVED_TOKENS = 4096


def frame_size(size, pixels):
    """The source size scaled down to about `pixels`, keeping its shape; never enlarged."""
    width, height = size
    if not width or not height:
        return 480, 270
    scale = min(1., math.sqrt(pixels / (width * height)))
    return max(2, round(width * scale / 2) * 2), max(2, round(height * scale / 2) * 2)


def video_plan(recording, chat):
    """(frames a second, (width, height)) for a recording's frames, so all of them fit the server."""
    seconds = recording.end + 1 / recording.fps
    if seconds <= SHORT_SECONDS:
        rate, pixels = SHORT_RATE, SHORT_PIXELS
    else:
        rate, pixels = max(LONG_RATE, LONG_FRAMES / seconds), LONG_PIXELS
    size = frame_size(recording.size, pixels)
    per_frame = math.ceil(size[0] / TOKEN_PIXELS) * math.ceil(size[1] / TOKEN_PIXELS) + TAG_TOKENS
    most = max(1, min(chat.max_images, (chat.context_tokens - RESERVED_TOKENS) // per_frame))
    return min(rate, most / seconds), size


class DirectSubtask:
    """The question and its frames, straight to the offline model."""

    def __init__(self, host):
        self.host = host

    def prepare(self, recording):
        """Start decoding a recording as soon as it is uploaded, so the first question need not wait."""
        recording.seconds(*video_plan(recording, self.host.settings.chat))

    async def run(self, request: OfflineRequest) -> OfflineResult:
        if request.input_mode == "offline":
            # Shielded: a question that runs out of time leaves the decode to the next one.
            video = request.recording
            seconds = await asyncio.shield(video.seconds(*video_plan(video, self.host.settings.chat)))
            frames = [frame for frame in seconds if frame["pts"] <= request.asked_at + 1e-6] or seconds[:1]
        elif request.route == "chat":
            frames = []
        else:
            frames = [request.current_frame] if request.current_frame is not None else []
        if request.route != "chat" and not frames:
            language = self.host.locale.pick(request.question)
            return OfflineResult(self.host.say(language, "no_usable_frames"), "insufficient_evidence")
        reply = await self.host.direct(request.question, frames, request.dialogue, request.deadline)
        return OfflineResult(reply.pop("text"), reply.pop("status"), "offline_direct", reply)


def build(factory, host) -> OfflineSubtask:
    return factory(host) if inspect.signature(factory).parameters else factory()


def load(spec, host) -> OfflineSubtask:
    """The subtask a "package.module:factory" spec names, or DirectSubtask for an empty one."""
    if not spec:
        return DirectSubtask(host)
    module, _, name = spec.partition(":")
    return build(getattr(importlib.import_module(module), name), host)
