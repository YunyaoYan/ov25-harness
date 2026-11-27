"""In-process stand-ins for the streaming engine and the offline model, so the harness runs with no GPU.

FakeStreaming mirrors the surface of ov_streaming.client.OVClient that the harness uses and emits the
engine's events in the engine's order. Its model is a few fixed rules:

    frames          silence
    a question      answered at once, naming how many frames were seen
    a watch request silence, then a response on the 5th following frame and every 10 frames after
    caption mode    every 3rd frame opens a caption branch

FakeChat answers OpenAI-style chat completion bodies: a route for a routing request, else a line that
says how many pictures it was given.
"""
import asyncio
import json
import re
import time
import uuid

from ov_streaming.client import OVResponseError
from ov_streaming.types import Event

# Notify / let me know / tell me when / alert me / remind; the Chinese words for remind and notify.
_WATCH = re.compile(r"\b(?:notify|let\s+me\s+know|tell\s+me\s+when|alert\s+me|remind)\b|\u63d0\u9192|\u901a\u77e5", re.I)
# Earlier / before / a moment ago; the Chinese word for just now.
_PAST = re.compile(r"\b(?:earlier|before|a\s+moment\s+ago)\b|\u521a\u624d", re.I)
# Greetings, who are you, simple arithmetic; the Chinese hello and who are you.
_CHAT = re.compile(r"^\s*(?:hi|hello|hey|good\s+(?:morning|afternoon|evening))\b|\bwho\s+are\s+you\b"
                   r"|\d+(?:\.\d+)?\s*[-+*/x]\s*\d+(?:\.\d+)?|\u4f60\u597d|\u4f60\u662f\u8c01", re.I)
_TARGET = re.compile(r"\b(?:when|if|once)\s+(?:(?:you|i)\s+see\s+|there\s+(?:is|are)\s+)?(?P<target>.+)", re.I)
_TARGET_END = re.compile(r"\s+(?:appears?|shows?\s+up|comes?\s+into\s+view)$|[\s.!?,;:\u3002\uff01\uff1f]+$", re.I)
# The first frame after a watch request that answers it, then every this many frames.
WATCH_FIRST, WATCH_EVERY = 5, 10
CAPTION_EVERY = 3


def _is_watch(text):
    return bool(_WATCH.search(text))


def _watch_target(text):
    match = _TARGET.search(text)
    target = match["target"] if match else _WATCH.sub("", text)
    while (cut := _TARGET_END.sub("", target)) != target:
        target = cut
    return target.strip() or "the target"


class FakeStreaming:
    """Stands in for ov_streaming.client.OVClient.

    responder(question, frames_seen, t) -> str | None overrides every answer to a question; None leaves
    the question unanswered (silence), and such a question is not treated as a watch request.
    delay is how long a branch takes to write its text.
    """

    kind = "fake"

    def __init__(self, settings=None, *, responder=None, delay=0.0):
        self.settings, self.responder, self.delay = settings, responder, delay
        self.sessions = []

    def session(self, **options):
        session = FakeSession(self, **options)
        self.sessions.append(session)
        return session


class FakeSession:
    """Stands in for ov_streaming.client.ClientSession; unknown options are accepted and ignored."""

    def __init__(self, owner, *, enable=None, max_responses=3, max_visual_frames=None,
                 compact_to_visual_frames=None, answer_commit="on_done", **_ignored):
        self.owner = owner
        self.enable = tuple(enable or ())
        self.max_responses = max_responses
        self.max_visual_frames = max_visual_frames
        self.compact_to_visual_frames = compact_to_visual_frames
        self.answer_commit = answer_commit
        self.session_id = None
        self.inputs = []                 # (input_id, messages) in arrival order
        self.frames_seen = 0
        self._opened = self._closed = False
        self._finished = False           # session.closed was emitted
        self._events = asyncio.Queue()
        self._inputs = asyncio.Queue()
        self._worker = None
        self._consumer = None
        self._consumer_done = asyncio.Event()
        self._consumer_done.set()
        self._input_ids = set()
        self._turns = []                 # frames of each input the source holds, oldest first
        self._t = 0.
        self._responses = {}             # running branches by response_id
        self._results = {}
        self._result_changed = asyncio.Event()
        self._uncommitted = set()
        self._next_response = 0
        self._watch = None               # [target, frames since the request]
        self._caption_start = None

    async def __aenter__(self):
        if self._opened:
            raise RuntimeError("client session is already open")
        problem = self._check_options()
        if problem:
            raise RuntimeError(f"OV server returned 400: {problem}")
        if self.compact_to_visual_frames is None:
            self.compact_to_visual_frames = self.max_visual_frames
        self._opened = True
        self.session_id = f"fake-{uuid.uuid4().hex[:12]}"
        self._worker = asyncio.create_task(self._run_inputs())
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        if exc_type is None:
            await self.close()
        else:
            await self.abort()

    def _check_options(self):
        if self.max_responses < 1:
            return "max_responses must be positive"
        if self.answer_commit not in ("on_done", "client"):
            return "answer_commit must be on_done or client"
        if self.max_visual_frames is None:
            if self.compact_to_visual_frames is not None:
                return "compact_to_visual_frames requires max_visual_frames"
        elif self.max_visual_frames < 1:
            return "max_visual_frames must be positive"
        elif not 1 <= (self.compact_to_visual_frames or self.max_visual_frames) <= self.max_visual_frames:
            return "compact_to_visual_frames must be between 1 and max_visual_frames"
        return None

    def _require_open(self):
        if self._closed or not self._opened:
            raise RuntimeError("client session is not open")

    async def send_messages(self, *, messages, input_id=None):
        self._require_open()
        input_id = input_id or uuid.uuid4().hex
        if input_id in self._input_ids:
            raise RuntimeError(f"OV server returned 400: duplicate input_id: {input_id}")
        turn = self._read(messages)
        self._input_ids.add(input_id)
        self.inputs.append((input_id, list(messages)))
        self._inputs.put_nowait((input_id, turn, time.perf_counter()))
        return input_id

    def _read(self, messages):
        roles = [message.get("role") for message in messages]
        if roles != ["user"] and not (roles == ["system", "user"] and not self._input_ids):
            raise RuntimeError("OV server returned 400: an input is one user message, "
                               "after a system message only in the first input")
        content = messages[-1]["content"]
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        parts, frames = [], 0
        for part in content:
            kind = part.get("type")
            if kind in ("image_url", "image_pil"):
                frames += 1
            elif kind != "text":
                raise RuntimeError(f"OV server returned 400: unsupported content part {kind!r}")
            parts.append(part)
        if self.compact_to_visual_frames is not None and frames > self.compact_to_visual_frames:
            raise RuntimeError("OV server returned 400: one input exceeds compact_to_visual_frames; split the input")
        return parts

    async def _run_inputs(self):
        while (item := await self._inputs.get()) is not None:
            try:
                await self._process(*item)
            finally:
                self._inputs.task_done()
        self._inputs.task_done()

    async def _process(self, input_id, parts, received):
        frames, decision = 0, None
        for part in parts:
            if part.get("timestamp_sec") is not None:
                self._t = float(part["timestamp_sec"])
            if part["type"] == "text":
                if (text := part.get("text", "").strip()) and decision is None:
                    decision = self._answer(text)
            else:
                frames += 1
                found = self._on_frame()
                if found and (decision is None or decision[0] == "caption"):
                    decision = found
        t = self._t
        response_id = None
        if decision is not None:
            response_id = await self._start(input_id, t, *decision)
        if self.max_visual_frames is not None:
            self._hold(input_id, frames, t)
        timing = {"server_preprocess_ms": 0., "input_queue_ms": 1000 * (time.perf_counter() - received),
                  "control_ms": 0.}
        self._emit(Event("input.done", self.session_id, input_id=input_id, response_id=response_id,
                         timestamp_sec=t, status="completed", control=decision[0] if decision else "silence",
                         metadata={"timing": timing}))

    def _answer(self, text):
        """The control and text for a question, or None for silence."""
        if self.owner.responder is not None:
            answer = self.owner.responder(text, self.frames_seen, self._t)
            return None if answer is None else ("response", answer)
        if _is_watch(text):
            self._watch = [_watch_target(text), 0]
            return None
        n = self.frames_seen
        return ("response", f"At {self._t:g} seconds: a quiet test scene, {n} frame{'' if n == 1 else 's'} "
                            f"seen so far. You asked: {text}")

    def _on_frame(self):
        """Count one frame; a due watch alert or caption, else None."""
        self.frames_seen += 1
        if self._caption_start is None:
            self._caption_start = self._t
        found = None
        if "caption" in self.enable and self.frames_seen % CAPTION_EVERY == 0:
            found = ("caption", f"At {self._caption_start:g} - {self._t:g} seconds: frame {self.frames_seen}.")
            self._caption_start = None
        if self._watch is not None:
            self._watch[1] += 1
            since = self._watch[1]
            if since >= WATCH_FIRST and (since - WATCH_FIRST) % WATCH_EVERY == 0:
                found = ("response", f"At {self._t:g} seconds: {self._watch[0]} is visible.")
        return found

    def _hold(self, input_id, frames, t):
        """Keep this input on the source; compact as the engine does when too many frames are held."""
        if frames + sum(self._turns) <= self.max_visual_frames:
            self._turns.append(frames)
            return
        room, start = self.compact_to_visual_frames - frames, len(self._turns)
        for index in range(len(self._turns) - 1, -1, -1):
            room -= self._turns[index]
            if room < 0:
                break
            start = index
        self._turns = [*self._turns[start:], frames]
        self._emit(Event("context.compacted", self.session_id, input_id=input_id, timestamp_sec=t,
                         metadata={"retained_turns": len(self._turns),
                                   "retained_visual_frames": sum(self._turns)}))

    async def _start(self, input_id, t, control, text):
        response_id = f"ov-response-{self.session_id}-{self._next_response:06d}"
        self._next_response += 1
        while len(self._responses) >= self.max_responses:
            await self._cancel(next(iter(self._responses)), "capacity")
        run = dict(input_id=input_id, response_id=response_id, t=t, control=control, finished=False)
        self._responses[response_id] = run
        self._emit(Event("response.created", self.session_id, input_id=input_id, response_id=response_id,
                         timestamp_sec=t, status="in_progress", control=control,
                         metadata={"verification": None}))
        run["task"] = asyncio.create_task(self._run_response(run, text))
        return response_id

    async def _run_response(self, run, text):
        try:
            await asyncio.sleep(self.owner.delay)
            if run["control"] == "response" and self.answer_commit == "client":
                self._uncommitted.add(run["response_id"])
            self._emit(Event("response.output_text.done", self.session_id, input_id=run["input_id"],
                             response_id=run["response_id"], timestamp_sec=run["t"], text=text,
                             control=run["control"], metadata={"timing": {}}))
            self._done(run, "completed")
        finally:
            self._responses.pop(run["response_id"], None)

    def _done(self, run, status, reason=None):
        run["finished"] = True
        event = Event("response.done", self.session_id, input_id=run["input_id"],
                      response_id=run["response_id"], timestamp_sec=run["t"], status=status, reason=reason,
                      control=run["control"], metadata={"timing": {}})
        self._results.setdefault(run["response_id"], event)
        self._result_changed.set()
        self._emit(event)

    async def _cancel(self, response_id, reason):
        run = self._responses.pop(response_id, None)
        if run is None:
            return False
        run["task"].cancel()
        await asyncio.gather(run["task"], return_exceptions=True)
        # A task cancelled before it starts never runs its own cleanup, so the end is reported here.
        if not run["finished"]:
            self._done(run, "cancelled", reason)
        return True

    def __aiter__(self):
        return self.events()

    async def events(self):
        if self._consumer is not None:
            raise RuntimeError("only one event consumer is allowed per session")
        if not self._opened:
            raise RuntimeError("client session is not open")
        self._consumer = asyncio.current_task()
        self._consumer_done.clear()
        try:
            while (event := await self._events.get()) is not None:
                yield event
                if event.type == "session.closed":
                    return
        finally:
            self._consumer = None
            self._consumer_done.set()

    async def responses(self):
        async for event in self.events():
            if event.type == "response.output_text.done":
                yield event

    def wait_response(self, response_id):
        """Wait for response.done while another task consumes events()."""
        async def wait():
            while response_id not in self._results:
                self._result_changed.clear()
                await self._result_changed.wait()
            event = self._results[response_id]
            if event.status != "completed":
                raise OVResponseError(event)
            return event

        return asyncio.create_task(wait())

    async def cancel_response(self, response_id):
        self._require_open()
        return await self._cancel(response_id, "user")

    async def commit_answer(self, response_id, text):
        """Accepts what the user received of an answer (answer_commit="client"); the fake keeps no memory."""
        self._require_open()
        if response_id not in self._uncommitted:
            return False
        self._uncommitted.discard(response_id)
        return True

    async def close(self):
        """Stop input and wait for inputs already sent and branches already created."""
        if self._closed or not self._opened:
            self._closed = True
            return
        self._closed = True
        await self._inputs.join()
        self._inputs.put_nowait(None)
        await self._worker
        await asyncio.gather(*(run["task"] for run in list(self._responses.values())), return_exceptions=True)
        await self._finish("completed")

    async def abort(self):
        """Cancel all work at once."""
        if self._closed or not self._opened:
            self._closed = True
            return
        self._closed = True
        self._worker.cancel()
        await asyncio.gather(self._worker, return_exceptions=True)
        for response_id in list(self._responses):
            await self._cancel(response_id, "session_aborted")
        await self._finish("cancelled")

    async def _finish(self, status):
        self._finished = True
        self._events.put_nowait(Event("session.closed", self.session_id, status=status))
        self._events.put_nowait(None)
        if self._consumer is not None and self._consumer is not asyncio.current_task():
            await self._consumer_done.wait()

    def _emit(self, event):
        if not self._finished:
            self._events.put_nowait(event)


class FakeChat:
    """Stands in for the offline model's OpenAI-compatible chat completions."""

    kind = "fake"

    def __init__(self, settings=None):
        self.settings = settings
        self.requests = []

    async def complete(self, body, timeout):
        self.requests.append(body)
        messages = body.get("messages") or []
        users = [message for message in messages if message.get("role") == "user"]
        parts = _parts(users[-1]["content"]) if users else []
        text = " ".join(part.get("text", "") for part in parts if part.get("type") == "text").strip()
        if "response_format" in body:
            try:
                utterance = json.loads(text).get("utterance", "")
            except (ValueError, AttributeError):
                utterance = text
            return json.dumps({"route": route(str(utterance))}), "stop"
        images = sum(part.get("type") == "image_url" for message in messages for part in _parts(message.get("content")))
        video = any(part.get("type") == "video_url" for message in messages for part in _parts(message.get("content")))
        seen = "a video" if video else f"{images} image(s)"
        return f"[fake offline model] saw {seen}: {text}", "stop"

    async def aclose(self):
        pass


def _parts(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return list(content or [])


def route(utterance):
    """The route FakeChat gives an utterance."""
    if _is_watch(utterance):
        return "watch"
    if _PAST.search(utterance):
        return "past"
    if _CHAT.search(utterance):
        return "chat"
    return "realtime"
