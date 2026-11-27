"""The live harness: judge and route each question once, then hand it over.

    live question ── router ─┬─ cancel   → stop the watch
                             ├─ watch    → the session's stream; the streaming model decides when to speak
                             ├─ past     → the session's stream, asked now; if it stays silent,
                             │             the offline model with frames from the session buffer
                             ├─ realtime → offline model with the current frame   ┐ offline_subtask=true:
                             └─ chat     → offline model, no frame                ┘ the offline subtask
    offline recording ──────────────────→ the offline subtask, no routing

The harness never rewrites a question or plans a task; that belongs to the offline subtask
(subtask.py). The question reaches every model in the user's own words.
"""
import asyncio
import base64
from collections import deque
import logging
import math
import re
import time
from uuid import uuid4

import httpx

from . import router, subtask as subtasks
from .alerts import parse_live_alert
from .stream import SENT_FRAMES, StreamSilent, StreamUnavailable, UserStream

_LOG = logging.getLogger(__name__)
# A stream that times out on a past question leaves the offline model at least this long.
PAST_FALLBACK_S = 3.
# How late the stream's own timer may be noticed and still count as leaving PAST_FALLBACK_S.
FALLBACK_SLACK_S = .5
# The largest timestamp a frame or question may carry: beyond any session clock, Unix time included.
# A larger one would only lock its session out of every later frame, or reach the model's prompt as
# a number of hundreds of digits.
MAX_T_S = 1e10
# The streaming model may open an answer with its own clock: "At 12 seconds: ...", or the same
# words in Chinese.
_TIME_PREFIX = re.compile(r"^(?:At\s+(\d+(?:\.\d+)?)\s*(?:seconds?|s)|\u7b2c?\s*(\d+(?:\.\d+)?)\s*\u79d2)\s*[:\uff1a]\s*",
                          re.I)
_ERRORS = (httpx.HTTPError, TimeoutError, KeyError, IndexError, ValueError)
# What ends an item of an answer, and what ends a sentence, in either language (the ideographic comma,
# full stop and full-width marks included).
_ITEM_END = ",;\uff0c\u3001\uff1b"
_SENTENCE_END = ".!?\n\u3002\uff01\uff1f"
_PIECES = re.compile(f"(?<=[{re.escape(_ITEM_END + _SENTENCE_END)}])")


def _reply(text, status="ok", **fields):
    return dict(text=text, status=status, **fields)


def answer_body(text):
    """The streaming model's answer without its clock prefix: (the seconds it named or None, the text)."""
    text = text.strip()
    match = _TIME_PREFIX.match(text)
    if match is None:
        return None, text
    seconds = float(match[1] or match[2])
    return (seconds if math.isfinite(seconds) else None), text[match.end():]


def trim_unfinished(text, etc):
    """An answer the model was cut off in, shortened to what it finished saying.

    The offline model writes until max_tokens when it loops over the same items ("a white bowl, a
    white bowl, ...") and when it describes at length; either way the text stops mid-phrase. A loop is
    cut where an item it keeps coming back to (three times or more) first comes round again, and
    etc(text) ends the list; anything else goes back to its last full sentence. A description may well
    name a thing twice.
    """
    pieces = _PIECES.split(text)
    items = [piece.strip().rstrip(_ITEM_END + _SENTENCE_END) for piece in pieces]
    looping = {item for item in items if item and items.count(item) >= 3}
    seen, kept = set(), []
    for piece, item in zip(pieces, items, strict=True):
        if item in looping and item in seen and kept:
            said = "".join(kept).rstrip()
            return said if said[-1:] in tuple(_SENTENCE_END) else etc(said.rstrip(_ITEM_END))
        seen.add(item)
        kept.append(piece)
    end = max(text.rfind(mark) for mark in _SENTENCE_END)
    if end > 0:
        return text[:end + 1].strip()
    end = max(text.rfind(mark) for mark in _ITEM_END)
    return (text[:end] if end > 0 else text).rstrip() + "\u2026"


def lock_current_frame(frames, at, submitted_at, max_age):
    """The frame a question about "now" is answered from, or None with the reason it has none."""
    if not frames:
        return None, {"status": "no_frame_at_or_before_question"}
    latest = frames[-1]
    source_age = at - latest["pts"]
    arrival_age = submitted_at - latest["received_at"]
    accepted = 0 <= source_age <= max_age and 0 <= arrival_age <= max_age
    check = dict(status="locked" if accepted else "stale_at_submission",
                 pts=latest["pts"], frame_index=latest["frame_index"],
                 source_age_s=source_age, arrival_age_s=arrival_age,
                 submitted_monotonic=submitted_at)
    return (latest if accepted else None), check


def sample_evenly(frames, budget):
    """Up to budget frames spread evenly over frames, oldest and newest included."""
    count = min(budget, len(frames))
    if count <= 0:
        return []
    if count == 1:
        return [frames[-1]]
    return [frames[round(i * (len(frames) - 1) / (count - 1))] for i in range(count)]


class Harness:
    """Routes and answers the questions of every session, with the offline model behind `chat`."""

    def __init__(self, chat, settings, locale, subtask=None):
        self.chat, self.settings, self.locale = chat, settings, locale
        # For subtasks that call other services; complete() bounds each call by the turn's
        # deadline, so httpx's own 5 s default would cut a slow answer short.
        self.http = httpx.AsyncClient(trust_env=False, timeout=None)
        if subtask is None or isinstance(subtask, str):
            self.subtask_name = subtask or settings.subtask.factory or "direct"
            self.subtask = subtasks.load(subtask or settings.subtask.factory, self)
        elif hasattr(subtask, "run"):
            self.subtask_name, self.subtask = type(subtask).__name__, subtask
        else:
            self.subtask_name = getattr(subtask, "__name__", type(subtask).__name__)
            self.subtask = subtasks.build(subtask, self)

    async def close(self):
        await self.http.aclose()

    def say(self, language, key, **fields):
        return self.locale.message(language, key, **fields)

    async def route(self, question, time_scope, deadline):
        if route := router.by_rule(question, self.locale, time_scope):
            return route, "rule"
        try:
            content, _ = await self.chat.complete(router.request(question, self.settings.chat.model, self.locale),
                                                  min(self.settings.policy.route_timeout_s, deadline - time.monotonic()))
            return router.parse(content), "model"
        except _ERRORS as error:
            # A question about the scene is the common case; answering it beats dropping it.
            _LOG.warning("route fell back to realtime: %s", type(error).__name__)
            return "realtime", "fallback"

    async def direct(self, question, frames, dialogue, deadline):
        """The question and its frames to the offline model as they are.

        Only a question without frames gets the persona and the conversation. With a picture the model
        applies the persona to scene questions too and introduces itself instead of answering, and it
        repeats its earlier answer to the same question instead of looking at the new frame.
        """
        language = self.locale.pick(question)
        messages = []
        if not frames:
            messages.append({"role": "system", "content": self.locale.persona(language)})
            for asked, answered in dialogue:
                messages += [{"role": "user", "content": asked}, {"role": "assistant", "content": answered}]
        content = []
        for frame in frames:
            # A video frame carries its time as the model was trained to read it, "<T.T seconds>" right
            # before the picture; a single picture (the current frame) has none.
            if frame.get("timestamp") is not None:
                content.append({"type": "text", "text": f"<{frame['timestamp']:.1f} seconds>"})
            content.append({"type": "image_url", "image_url": {"url": _data_url(frame["png"])}})
        messages += [{"role": "user", "content": [*content, {"type": "text", "text": question}]}]
        # The template's own assistant turn: OV2.5 is trained on the qwen2-vl format, which has no think
        # mode, and a prefilled "<think></think>" makes it reason aloud past the deadline.
        model = self.settings.chat.model
        body = {"model": model, "temperature": 0, "max_tokens": 256, "messages": messages}
        try:
            text, finish = await self.chat.complete(body, deadline - time.monotonic())
        except _ERRORS as error:
            _LOG.warning("direct answer failed: %s", type(error).__name__)
            return _reply(self.say(language, "offline_model_no_answer"), "upstream_unavailable")
        text = text.strip()
        if finish == "length" and text:
            text = trim_unfinished(text, lambda said: self.say(language, "list_continues", text=said))
        if not text:
            return _reply(self.say(language, "no_usable_answer"), "invalid_visual_format")
        fields = dict(backend="offline_model", vision_model=model, finish_reason=finish)
        if frames:
            fields.update(source_window_s=[frames[0]["pts"], frames[-1]["pts"]])
            if len(frames) > 8:
                fields.update(video_frames=len(frames))
            else:
                fields.update(source_frame_indices=[frame.get("frame_index") for frame in frames])
        return _reply(text, **fields)

    async def process(self, state):
        session = state["session"]
        if state.get("input_mode") == "offline":
            return await self.handoff(state, None)
        route, decided_by = await self.route(state["question"], state.get("time_scope"), state["deadline"])
        language = state["language"]
        if route == "cancel":
            stopped = await session.stop_watch()
            result = _reply(self.say(language, "stopped" if stopped else "nothing_to_stop"))
        elif route == "watch":
            result = await session.watch_in_stream(state["question"], state["at"], language)
        elif route == "past":
            result = await self.ask_stream(state)
        elif session.offline_subtask:
            return await self.handoff(state, route)
        elif route == "chat":
            result = await self.direct(state["question"], [], state["chat"], state["deadline"])
        elif state.get("current_frame") is None:
            check = state.get("current_frame_check") or {}
            result = _reply(self._no_current_frame(language, check), "insufficient_evidence",
                            current_frame_check=check)
        else:
            result = await self.direct(state["question"], [state["current_frame"]], state["chat"], state["deadline"])
        result["route_decided_by"] = decided_by
        return {"route": route, "task": route, "result": result}

    async def ask_stream(self, state):
        """A past question: the session's stream answers it, or the session buffer stands in."""
        stream, language = state["session"].stream, state["language"]
        if stream is None:
            return _reply(self.say(language, "stream_unavailable_past"), "stream_unavailable")
        try:
            # The stream gets the turn less what the offline model needs to stand in for it.
            raw = await stream.ask(state["question"], state["at"],
                                   state["deadline"] - time.monotonic() - PAST_FALLBACK_S)
        except StreamSilent as silent:
            start = silent.window_start if silent.window_start is not None else stream.window_start()
            return await self.past_from_buffer(state, start, "stream_silent")
        except TimeoutError as error:
            # The stream's timer fires PAST_FALLBACK_S before the deadline, and is noticed a little
            # after that: asking for the whole PAST_FALLBACK_S here would never fall back.
            if state["deadline"] - time.monotonic() >= PAST_FALLBACK_S - FALLBACK_SLACK_S:
                # The start when the question was decided: frames sent since may have compacted the
                # source. Never decided, no later compaction has been seen yet either.
                start = getattr(error, "window_start", None)
                start = stream.window_start() if start is None else start
                return await self.past_from_buffer(state, start, "stream_timeout")
            _LOG.warning("stream answer failed: %s", type(error).__name__)
            return _reply(self.say(language, "stream_no_answer"), "stream_unavailable")
        except StreamUnavailable as error:
            _LOG.warning("stream answer failed: %s", error)
            return _reply(self.say(language, "stream_no_answer"), "stream_unavailable")
        _, text = answer_body(raw["answer"])
        window = raw["source_window_s"]
        return _reply(text, backend="streaming_session", source_window_s=window,
                      requested_window_s=window, coverage="stream",
                      native_receipt={k: raw[k] for k in ("qa_session_id", "qa_input_id", "qa_response_id")})

    async def past_from_buffer(self, state, start, reason):
        """The offline model on frames from the session buffer between the source window's start
        and the question, the question unchanged and no dialogue."""
        frames = [frame for frame in state["frames"] if start is None or frame["pts"] >= start]
        frames = sample_evenly(frames, self.settings.chat.past_images)
        if not frames:
            return _reply(self.say(state["language"], "no_usable_frames"), "insufficient_evidence",
                          fallback_reason=reason)
        result = await self.direct(state["question"], frames, (), state["deadline"])
        result.update(coverage="buffer", fallback_reason=reason)
        return result

    async def handoff(self, state, route):
        session = state["session"]
        request = subtasks.OfflineRequest(
            question=state["question"], asked_at=state["at"], input_mode=state.get("input_mode", "live"),
            frames=tuple(state["frames"]), current_frame=state.get("current_frame"),
            recording=session.recording, max_pixels=session.max_pixels, dialogue=tuple(state["chat"]),
            route=route, deadline=state["deadline"])
        try:
            answer = await self.subtask.run(request)
        except Exception as error:   # the subtask is someone else's code; keep the session alive
            _LOG.warning("offline subtask failed: %s", type(error).__name__)
            return {"route": "offline", "task": "offline_subtask",
                    "result": _reply(self.say(state["language"], "temporarily_unavailable"), "upstream_unavailable")}
        return {"route": "offline", "task": answer.task, "result": _reply(answer.text, answer.status, **answer.fields)}

    def _no_current_frame(self, language, check):
        """Why there is no frame to answer from, in words the user can act on."""
        if "pts" not in check:
            return self.say(language, "no_frame_yet")
        age = max(check["source_age_s"], check["arrival_age_s"])
        return self.say(language, "stale_frame", age=f"{age:.0f}")


def _data_url(data):
    media_type = "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"
    return f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}"


class LiveStream(UserStream):
    """The session's streaming source, also watching: a question left in it without forcing an
    answer is answered by the streaming model when it decides the moment has come."""

    def __init__(self, runtime, audit, pool):
        super().__init__(runtime, audit, pool)
        self.watching = None

    def sent(self, input_id, pts):
        super().sent(input_id, pts)
        if self.watching is not None:
            inputs = self.watching["inputs"]
            inputs[input_id] = None
            if len(inputs) > SENT_FRAMES:
                del inputs[next(iter(inputs))]

    def observe(self, event):
        super().observe(event)
        data = event.to_dict()
        watch = self.watching
        input_id, kind, control = data.get("input_id"), data["type"], data.get("control")
        # Only the current watch's own question and frames can trigger its reports.
        if watch is None or input_id not in watch["inputs"]:
            return
        if kind in {"response.done", "response.output_text.done"} or (kind == "input.done" and control != "response"):
            watch["inputs"].pop(input_id)
        if kind != "response.output_text.done" or control != "response":
            return
        _, text = answer_body(data.get("text") or "")
        if not text.strip():
            return
        at = data.get("timestamp_sec")
        if not watch["repeat"]:
            self.watching = None
        self.runtime.session.publish(at, "watch", "watch", _reply(
            text.strip(), "model_report", source_window_s=[at, at], backend="streaming_session"))

    def fail(self, reason):
        watch, self.watching = self.watching, None
        super().fail(reason)
        if watch is not None and reason != "closed":
            session = self.runtime.session
            session.publish(None, "watch", "watch", _reply(
                session.harness.say(session.language, "watch_interrupted"), "upstream_unavailable"))

    async def watch(self, question, at):
        runtime = self.runtime
        async with runtime.frame_lock:
            source = runtime.source
            if source is None or self.failure is not None:
                raise StreamUnavailable(self.failure or "detached")
            seen = runtime.session.frames[-1]["pts"] if runtime.session.frames else 0.
            part = dict(type="text", text=question, timestamp_sec=max(float(at), seen))
            alert = parse_live_alert(question, runtime.session.harness.locale)
            # Watch before sending, so an answer to the question itself is not missed.
            input_id = uuid4().hex
            watch = self.watching = dict(repeat=bool(alert and alert.repeat), inputs={input_id: None})
            self.mark(input_id)
            try:
                await source.send_messages(messages=[dict(role="user", content=[part])], input_id=input_id)
            except BaseException:
                if self.watching is watch:
                    self.watching = None
                raise


class LiveSession:
    """The harness side of one client session: its frames, its conversation and its running question."""

    def __init__(self, harness, emit, fps, max_pixels, voice, *, input_mode="live", offline_subtask=False):
        self.harness, self.emit, self.fps, self.max_pixels = harness, emit, fps, max_pixels
        self.policy = harness.settings.policy
        self.voice = voice
        self.input_mode = input_mode
        self.offline_subtask = offline_subtask
        # The language of the last question: messages that answer no question use it.
        self.language = harness.locale.default
        self.recording = None
        self.stream = None
        self.frames = deque()
        self.frame_bytes = 0
        self.chat = deque(maxlen=self.policy.dialogue_turns)
        self.pending_chat = {}
        self.request = None
        self.requests = set()
        self.closed = False

    def use_recording(self, recording):
        """The uploaded recording; the subtask may start reading it before the first question."""
        self.recording = recording
        prepare = getattr(getattr(self.harness, "subtask", None), "prepare", None)
        if prepare is not None:
            prepare(recording)

    def frame(self, data, pts, index):
        if self.closed or len(data) > self.policy.max_frame_bytes or self.frames and pts <= self.frames[-1]["pts"]:
            return
        self.frames.append(dict(png=data, pts=pts, frame_index=index, fps=self.fps, pixel_budget=self.max_pixels,
                                received_at=time.monotonic()))
        self.frame_bytes += len(data)
        while self.frame_bytes > self.policy.buffer_bytes or len(self.frames) > self.policy.buffer_frames:
            self.frame_bytes -= len(self.frames.popleft()["png"])

    def observe(self, at):
        return time.monotonic(), tuple(frame for frame in self.frames if frame["pts"] <= at)

    def submit(self, question, at, *, observation=None, time_scope=None):
        if self.input_mode == "offline":
            if self.recording is None:
                raise ValueError("upload the recording first")
            if at is None:
                at = self.recording.end
            elif self.recording.end < at < self.recording.end + 1 / self.recording.fps + 1e-6:
                at = self.recording.end
            elif at > self.recording.end:
                raise ValueError("playback timestamp exceeds the recording")
        # (A NaN fails the comparison too.)
        if self.closed or not question.strip() or len(question) > 2000 or at is None or not 0 <= at <= MAX_T_S:
            raise ValueError("invalid question")
        self.cancel_request()
        submitted_at, frames = observation if observation is not None else self.observe(at)
        policy = self.policy
        deadline = time.monotonic() + (policy.offline_turn_s if self.input_mode == "offline" else policy.live_turn_s)
        self.language = self.harness.locale.pick(question)
        selected = {}
        if self.input_mode == "live":
            current, check = lock_current_frame(frames, at, submitted_at, policy.current_frame_age_s)
            selected = dict(current_frame=current, current_frame_check=check)
        self.request = asyncio.create_task(self.run(dict(
            session=self, question=question.strip(), at=at, frames=frames, chat=tuple(self.chat),
            input_mode=self.input_mode, deadline=deadline, time_scope=time_scope, language=self.language,
            **selected)))
        self.requests.add(self.request)
        self.request.add_done_callback(self.requests.discard)

    def cancel_request(self):
        if self.request is not None and not self.request.done() and not self.request.cancelling():
            self.request.cancel()

    async def run(self, state):
        started = time.monotonic()
        language = state["language"]
        try:
            async with asyncio.timeout(state["deadline"] - started):
                answer = await self.harness.process(state)
        except TimeoutError:
            if asyncio.current_task() is self.request:
                self.publish(state["at"], None, None, _reply(self.harness.say(language, "request_timeout"),
                                                             "request_timeout"))
            return
        except Exception as error:
            _LOG.warning("live harness query failed: %s", type(error).__name__)
            if asyncio.current_task() is self.request:
                self.publish(state["at"], None, None, _reply(self.harness.say(language, "temporarily_unavailable"),
                                                             "upstream_unavailable"))
            return
        if asyncio.current_task() is not self.request:
            return
        result = answer["result"]
        # Every answer the user gets is part of the conversation the next direct answer sees.
        remembered = answer["route"] not in {"watch", "cancel"} and result.get("status") == "ok"
        self.publish(state["at"], answer["route"], answer["task"], result, state["question"] if remembered else None,
                     elapsed_s=round(time.monotonic() - started, 3))

    def publish(self, at, route, task, result, question=None, **metadata):
        """Send one result row to the client; with a question, the pair joins the conversation."""
        if self.closed:
            return
        response_id = "harness-" + uuid4().hex
        if question is not None:
            native_id = result.get("native_receipt", {}).get("qa_response_id") if route == "past" else None
            if self.voice:
                # A spoken answer is remembered as what the user heard, once the audio socket
                # reports it: see commit().
                self.pending_chat[response_id] = (question, native_id)
                if len(self.pending_chat) > 16:
                    self.commit(next(iter(self.pending_chat)), "")
            else:
                self.chat.append((question, result["text"]))
                self.commit_stream_answer(native_id, result["text"])
        anchor = result.get("source_window_s", [at])[-1]
        self.emit(dict(type="answer", t=anchor, response_id=response_id, route=route, task=task,
                       **result, **metadata))

    def commit(self, response_id, heard_text):
        pending = self.pending_chat.pop(response_id, None)
        if pending is None:
            return
        question, native_id = pending
        if heard_text.strip():
            self.chat.append((question, heard_text))
        self.commit_stream_answer(native_id, heard_text)

    def commit_stream_answer(self, native_id, text):
        if native_id is not None and self.stream is not None:
            self.stream.commit_answer(native_id, text)

    async def watch_in_stream(self, question, at, language):
        unavailable = _reply(self.harness.say(language, "stream_unavailable_watch"), "stream_unavailable")
        if self.stream is None:
            return unavailable
        await self.stop_watch()
        try:
            await self.stream.watch(question, at)
        except StreamUnavailable:
            return unavailable
        return _reply(self.harness.say(language, "watch_accepted"), "watch_accepted")

    async def stop_watch(self):
        """Stop the watch left in the stream; whether there was one."""
        watching = self.stream is not None and self.stream.watching is not None
        if watching:
            self.stream.watching = None
        return watching

    async def close(self):
        self.closed = True
        requests = tuple(self.requests)
        for request in requests:
            if not request.cancelling():
                request.cancel()
        try:
            await asyncio.gather(*requests, return_exceptions=True)
            await self.stop_watch()
            if self.recording is not None and hasattr(self.recording, "forget"):
                await self.recording.forget()
            self.frames.clear()
            self.pending_chat.clear()
            self.chat.clear()
        finally:
            # The runtime closes the source itself; the harness side gives back its slot here, or the
            # app refuses new live sessions once `user_streams` of them have been closed.
            if self.stream is not None:
                self.stream.fail("closed")
                self.stream.release()
