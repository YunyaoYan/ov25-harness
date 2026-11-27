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

from dataclasses import dataclass, field
import importlib
import inspect
from typing import Any, Protocol


@dataclass(frozen=True)
class OfflineRequest:
    question: str              # the user's words, unchanged
    asked_at: float            # session seconds the question refers to
    input_mode: str            # "live" or "offline"
    frames: tuple              # live: frames received up to asked_at, oldest first
                               #   (dicts with png, pts, frame_index, fps)
    current_frame: dict | None  # live: the newest frame if it is fresh enough to be "now"
    recording: Any             # offline: the uploaded Recording (indices(), rgb(), end, fps, path)
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


class DirectSubtask:
    """The question and its frames, straight to the offline model."""

    def __init__(self, host):
        self.host = host

    async def run(self, request: OfflineRequest) -> OfflineResult:
        if request.input_mode == "offline":
            video = request.recording
            indices = video.indices(0., min(request.asked_at, video.end), self.host.settings.chat.max_images)
            frames = await video.rgb(indices, request.max_pixels) if indices else []
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
