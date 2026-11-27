"""Each live session's own streaming source, on the harness side.

The app opens that source when a live session opens and sends it every accepted frame. The harness
only chooses which questions go to it; everything else keeps its own path.

Where the source's window starts is worked out here, from the frames sent to it and the engine's
``context.compacted`` events: the engine itself does not say.
"""
import asyncio
from collections import OrderedDict, deque
from contextlib import suppress
from uuid import uuid4

# Responses a caption source keeps at once. A structured caption takes seconds to write,
# and with one branch the next frame's caption cancels it. Each costs an engine slot.
CAPTION_RESPONSES = 3
OPEN_S = 5.
COMMIT_S = 1.
# Question input_ids given up on (timed out or superseded), remembered so their late answers are
# not taken for something else.
ABANDONED = 64
# The pts of the frames sent to a source, newest last: more than a source ever keeps.
SENT_FRAMES = 4096
_background_tasks = set()


class StreamUnavailable(RuntimeError):
    pass


class StreamSilent(RuntimeError):
    """The streaming model took the question and chose not to answer it now."""

    def __init__(self, control="silence", window_start=None):
        super().__init__(control)
        # Where the source's window started when it took the question.
        self.window_start = window_start


class StreamTimeout(TimeoutError):
    """The question got no complete answer in time."""

    def __init__(self, window_start=None):
        super().__init__("no answer in time")
        # Where the source's window started when it took the question, None if it never decided it.
        # Frames sent after the question may compact the source while its answer is written.
        self.window_start = window_start


class StreamPool:
    """The live sessions' share of the engine. A source holds max_responses + 2 of the engine's
    request slots for the whole session, so one app opens at most `limit` of them."""

    def __init__(self, limit):
        self.limit = limit
        self.held = 0


async def open_user_stream(pool, client, *, audit, **options):
    """Open the streaming source of a live session, or None when there is no capacity for it."""
    if pool.held >= pool.limit:
        audit("user_stream", status="skipped", reason="limit", held=pool.held)
        return None
    pool.held += 1
    if options.get("enable"):
        options["max_responses"] = CAPTION_RESPONSES
    try:
        session = client.session(**{"enable": [], **options})
        async with asyncio.timeout(OPEN_S):
            await session.__aenter__()
    except Exception as error:
        pool.held -= 1
        audit("user_stream", status="unavailable", reason=type(error).__name__, detail=str(error)[:120])
        return None
    except BaseException:
        # A cancelled open gives its slot back too, or the pool shrinks by one for good.
        pool.held -= 1
        raise
    audit("user_stream", status="opened", session_id=session.session_id)
    return session


def drop_user_stream(runtime, reason):
    """Detach a failed source so the harness keeps answering without it."""
    stream = getattr(runtime.session, "stream", None)
    if stream is None or runtime.source is None:
        return False
    source, runtime.source = runtime.source, None
    runtime.session.stream = None
    runtime._second_of_input.clear()
    stream.fail(reason if isinstance(reason, str) else type(reason).__name__)
    stream.release()

    async def abort():
        with suppress(Exception):
            async with asyncio.timeout(OPEN_S):
                await source.abort()

    _background(abort())
    return True


def _background(coroutine):
    task = asyncio.create_task(coroutine)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


class UserStream:
    """The harness side of one live session's streaming source."""

    def __init__(self, runtime, audit, pool):
        self.runtime, self.audit, self.pool = runtime, audit, pool
        self.waiting = {}
        self.abandoned = OrderedDict()
        self.failure = None
        self.released = False
        # The pts of every frame sent, in order, and how many have been sent; an input_id maps to
        # the count at the time it was sent, which is where a compaction it triggered counts from.
        self.sent_pts = deque(maxlen=SENT_FRAMES)
        self.sent_count = 0
        self.marks = OrderedDict()
        # The index (in sent order) of the oldest frame the source still holds.
        self.start_index = 0

    def release(self):
        if not self.released:
            self.released = True
            self.pool.held -= 1

    def fail(self, reason):
        if self.failure is None:
            self.failure = reason
            self.audit("user_stream", status="closed" if reason == "closed" else "dropped", reason=reason,
                       frames=self.runtime.frames)
        for slot in self.waiting.values():
            slot["done"].set()

    def mark(self, input_id):
        self.marks[input_id] = self.sent_count
        while len(self.marks) > SENT_FRAMES:
            self.marks.popitem(last=False)

    def sent(self, input_id, pts):
        """A frame is being sent to the source as input_id."""
        self.sent_pts.append(pts)
        self.sent_count += 1
        self.mark(input_id)

    def window_start(self):
        """The pts of the oldest frame the source still holds, or None before the first frame."""
        offset = self.sent_count - len(self.sent_pts)
        index = max(self.start_index, offset)
        return self.sent_pts[index - offset] if index < self.sent_count else None

    def abandon(self, input_id, slot):
        """Give up on a question: its answer, if one still comes, belongs to no one."""
        self.abandoned[input_id] = None
        while len(self.abandoned) > ABANDONED:
            self.abandoned.popitem(last=False)
        decision = slot.get("decision") or {}
        # Only an answer is cancelled; a caption branch the question opened is the session's own.
        if decision.get("response_id") and decision.get("control") == "response":
            self.cancel(decision["response_id"])

    def cancel(self, response_id):
        source = self.runtime.source
        if source is None:
            return

        async def cancel():
            with suppress(Exception):
                async with asyncio.timeout(OPEN_S):
                    await source.cancel_response(response_id)

        _background(cancel())

    def commit_answer(self, response_id, text):
        """Write a delivered answer back before the next input, without delaying its publication."""
        async def commit():
            async with self.runtime.frame_lock:
                source = self.runtime.source
                if source is None or self.runtime.closing:
                    return
                try:
                    async with asyncio.timeout(COMMIT_S):
                        await source.commit_answer(response_id, text)
                except Exception as error:
                    self.audit("user_stream", status="commit_failed", reason=type(error).__name__)

        _background(commit())

    def observe(self, event):
        """Called by the runtime's reader for every event of the source."""
        data = event.to_dict()
        input_id = data.get("input_id")
        if data["type"] == "context.compacted" and isinstance(data.get("retained_visual_frames"), int):
            # The source now holds the newest K frames up to the input that compacted it.
            end = self.marks.get(input_id, self.sent_count)
            self.start_index = max(self.start_index, end - max(0, data["retained_visual_frames"]))
        elif data["type"] == "input.done":
            self.marks.pop(input_id, None)
            if input_id in self.abandoned and data.get("response_id") and data.get("control") == "response":
                self.cancel(data["response_id"])
        slot = self.waiting.get(input_id)
        if slot is None:
            return
        if data["type"] == "input.done":
            slot["decision"] = data
            # The engine takes inputs in order: every compaction an earlier input (or the question
            # itself) caused has been seen by now, and none a later one causes has. Read at sending,
            # the start would miss a compaction by a frame still being decided.
            slot["start"] = self.window_start()
            # Only an answer is waited for; any other control, caption included, is no answer.
            if data.get("control") != "response" or not data.get("response_id"):
                slot["done"].set()
        elif data["type"] == "response.output_text.done":
            slot["text"] = data.get("text") or ""
        elif data["type"] == "response.done":
            slot["terminal"] = data
            slot["done"].set()

    async def ask(self, question, at, timeout):
        """The source's answer to a question about every frame it has been sent.

        The question goes in as an ordinary one: the streaming model answers it when it is asked,
        or stays silent (StreamSilent). Nothing forces an answer.
        """
        runtime = self.runtime
        if self.failure is not None or not runtime.frames:
            raise StreamUnavailable(self.failure or "no frames")
        input_id = uuid4().hex
        slot = self.waiting[input_id] = dict(done=asyncio.Event(), text="")
        try:
            async with asyncio.timeout(timeout):
                # Frames are sent under this lock, so none can land between the
                # timestamp chosen here and the question.
                async with runtime.frame_lock:
                    source = runtime.source
                    if source is None or self.failure is not None:
                        raise StreamUnavailable(self.failure or "detached")
                    seen = runtime.session.frames[-1]["pts"] if runtime.session.frames else 0.
                    part = dict(type="text", text=question, timestamp_sec=max(float(at), seen))
                    self.mark(input_id)
                    await source.send_messages(messages=[dict(role="user", content=[part])], input_id=input_id)
                await slot["done"].wait()
        except BaseException as error:
            # Timed out or superseded: its answer, if one comes, must not be read as a watch alert.
            if self.failure is None:
                self.abandon(input_id, slot)
            if isinstance(error, TimeoutError):
                raise StreamTimeout(slot.get("start")) from error
            raise
        finally:
            self.waiting.pop(input_id, None)
        decision, terminal = slot.get("decision"), slot.get("terminal")
        if self.failure is not None or not decision:
            raise StreamUnavailable(self.failure or "no decision")
        start = slot.get("start")
        if decision.get("control") != "response":
            raise StreamSilent(decision.get("control") or "silence", start)
        if (not terminal or terminal.get("status") != "completed" or terminal.get("reason") == "length"
                or terminal.get("response_id") != decision.get("response_id") or not slot["text"].strip()):
            raise StreamUnavailable("invalid completion")
        return dict(answer=slot["text"], qa_session_id=source.session_id, qa_input_id=input_id,
                    qa_response_id=decision["response_id"], control=decision["control"],
                    source_window_s=[seen if start is None else start, seen], observed_frames=runtime.frames)
