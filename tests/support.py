"""What the tests share: the Chinese fixtures, settings, and scriptable stand-ins for the streaming
engine and the offline model that answer the way the real ones do."""
import asyncio
import json
from pathlib import Path
import tomllib
from types import SimpleNamespace
import uuid

from ov_streaming.types import Event

from ov25_harness import config
from ov25_harness.locale import Locale

DATA = Path(__file__).resolve().parent / "data"
ROOT = DATA.parents[1]
JPEG = b"\xff\xd8\xff" + bytes(64)
LOCALE = Locale(["en", "zh"])
# A question the engine leaves undecided, or opens an answer to that never finishes.
HOLD, OPEN = "hold", "open"


def fixture(name):
    with (DATA / "zh" / f"{name}.toml").open("rb") as handle:
        return tomllib.load(handle)


ZH = fixture("phrases")
Q, A = ZH["questions"], ZH["answers"]


def say(key, language="zh", **fields):
    return LOCALE.message(language, key, **fields)


def settings(**sections):
    """Settings with both languages active and the given sections changed."""
    data = {"server": {"languages": ["en", "zh"]}}
    for name, values in sections.items():
        data[name] = {**data.get(name, {}), **values}
    return config.Settings.model_validate(data)


def event(kind, input_id=None, at=None, session_id="s", **fields):
    """An engine event; fields Event has no slot for go to its metadata, as the engine sends them."""
    known = {key: fields.pop(key) for key in ("response_id", "text", "status", "reason", "control") if key in fields}
    return Event(type=kind, session_id=session_id, input_id=input_id, timestamp_sec=at, metadata=fields, **known)


class Source:
    """One engine session. Frames are silent, or captioned when the engine has a caption. A question
    gets what engine.reply says: text answers it at once, None stays silent, HOLD leaves it
    undecided and OPEN opens an answer that never finishes. Events come in the engine's order."""

    def __init__(self, engine, **options):
        self.engine, self.options = engine, options
        self.session_id = "src-" + uuid.uuid4().hex[:8]
        self.queue = asyncio.Queue()
        self.sent, self.held, self.cancelled = [], [], []
        self.committed = []
        self.closed = self.aborted = False

    async def __aenter__(self):
        if self.engine.fail_open:
            raise RuntimeError("OV server returned 503: the engine has no free request slots")
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        self.queue.put_nowait(None)

    async def abort(self):
        self.aborted = True
        await self.__aexit__()

    async def cancel_response(self, response_id):
        self.cancelled.append(response_id)
        return True

    async def commit_answer(self, response_id, text):
        self.committed.append((response_id, text))
        return True

    def emit(self, kind, input_id=None, at=None, **fields):
        self.queue.put_nowait(event(kind, input_id, at, self.session_id, **fields))

    def respond(self, input_id, text, at=None, *, control="response", status="completed", finish=True):
        response_id = "resp-" + uuid.uuid4().hex[:8]
        self.emit("response.created", input_id, at, response_id=response_id)
        self.emit("input.done", input_id, at, response_id=response_id, control=control)
        if finish:
            self.emit("response.output_text.done", input_id, at, response_id=response_id, text=text, control=control)
            self.emit("response.done", input_id, at, response_id=response_id, status=status, reason="stop")
        return response_id

    async def send_messages(self, *, messages, input_id=None):
        input_id = input_id or uuid.uuid4().hex
        part = messages[-1]["content"][0]
        at = part.get("timestamp_sec")
        self.sent.append(messages)
        reply = self.engine.reply
        if part["type"] == "image_url":
            if self.engine.caption:
                self.respond(input_id, self.engine.caption, at, control="caption")
            else:
                self.emit("input.done", input_id, at, control="silence")
        elif reply == HOLD:
            self.held.append(input_id)
        elif reply == OPEN:
            self.held.append(input_id)
            self.respond(input_id, "", at, finish=False)
        elif reply is None:
            self.emit("input.done", input_id, at, control="silence")
        else:
            self.respond(input_id, reply, at, status=self.engine.status)
        return input_id

    def parts(self, kind):
        return [messages[-1]["content"][0] for messages in self.sent if messages[-1]["content"][0]["type"] == kind]

    async def events(self):
        while (item := await self.queue.get()) is not None:
            yield item


class Engine:
    """The streaming engine: session(**options) -> Source."""

    kind = "test"

    def __init__(self, reply=None):
        self.reply, self.caption, self.status, self.fail_open = reply, None, "completed", False
        self.sources = []

    def session(self, **options):
        self.sources.append(Source(self, **options))
        return self.sources[-1]


class Chat:
    """The offline model: routing requests take the next of `routes`, anything else gets `answer`."""

    kind = "test"

    def __init__(self, answer="", routes=()):
        self.answer, self.routes, self.bodies = answer, list(routes), []
        self.error = self.route_error = None
        self.delay = 0.

    async def complete(self, body, timeout):
        self.bodies.append(body)
        if "response_format" in body:
            if self.route_error is not None:
                raise self.route_error
            return json.dumps({"route": self.routes.pop(0)}), "stop"
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.answer, "stop"

    def images(self, body=None):
        body = body or self.bodies[-1]
        return sum(part.get("type") == "image_url" for message in body["messages"]
                   if isinstance(message["content"], list) for part in message["content"])


def quiet(*args, **kwargs):
    """An audit that writes nowhere."""


def harness_stub():
    """What a session and its stream use of the harness."""
    return SimpleNamespace(locale=LOCALE, settings=settings(), say=LOCALE.message)
