# Offline subtask plugins

The harness itself only routes a question and hands it over. When a question needs more than one
plain call to the offline model (rewriting it, choosing frames, watching a whole recording as a
video, calling another service), that work belongs to the **offline subtask**: a small Python
object you can replace without touching the harness.

## When the subtask runs

| Question | Goes to the subtask? |
| --- | --- |
| Any question about an uploaded recording (`input_mode: "offline"`) | Always, without routing |
| A live `realtime` or `chat` question in a session opened with `"offline_subtask": true` | Yes |
| A live `realtime` or `chat` question otherwise | No: the offline model answers directly |
| A live `past`, `watch` or `cancel` question | Never: those stay with the streaming source |

The subtask answers once and returns. It never watches a stream; anything continuous stays in the
harness.

## The interface

Everything is in `ov25_harness.subtask`.

### `OfflineSubtask`

Any object with one coroutine method:

```python
class OfflineSubtask(Protocol):
    async def run(self, request: OfflineRequest) -> OfflineResult: ...
```

### `OfflineRequest` (frozen dataclass)

| Field | Type | Meaning |
| --- | --- | --- |
| `question` | `str` | The user's words, trimmed and otherwise unchanged |
| `asked_at` | `float` | Session seconds the question refers to. For a recording without `t`, the recording's end |
| `input_mode` | `str` | `"live"` or `"offline"` |
| `frames` | `tuple[dict, ...]` | Live: buffered frames received up to `asked_at`, oldest first. Empty for recordings |
| `current_frame` | `dict \| None` | Live: the newest frame if it is fresh enough to count as "now", else `None` |
| `recording` | `Recording \| None` | Offline: the uploaded recording (below) |
| `max_pixels` | `int` | Pixel budget for frames decoded from a recording |
| `dialogue` | `tuple[tuple[str, str], ...]` | `(question, answer)` pairs the user has received, oldest first |
| `route` | `str \| None` | The router's judgment, `"realtime"` or `"chat"`; `None` for recordings. Advisory |
| `deadline` | `float` | `time.monotonic()` value by which the answer must be back |

A frame is a dict with `png` (the image bytes: as received for live frames, JPEG or PNG despite the
name), `pts` (session seconds), `frame_index`, `fps`, `pixel_budget`, and for live frames
`received_at` (`time.monotonic()` at arrival).

A `Recording` has `path`, `fps`, `timestamps` (each frame's time in seconds, from 0), `end` (the
last frame's time), and two methods:

- `indices(start, end, budget) -> list[int]`: up to `budget` frame indices spread evenly between two
  times;
- `await rgb(indices, pixel_budget) -> list[dict]`: those frames decoded with ffmpeg into PNG
  (scaled down to at most 768 pixels on each side), as frame dicts.

### `OfflineResult` (dataclass)

| Field | Default | Meaning |
| --- | --- | --- |
| `text` | | The answer, or a message for the user |
| `status` | `"ok"` | A status from [api.md](api.md#statuses), e.g. `ok`, `insufficient_evidence`, `upstream_unavailable` |
| `task` | `"offline_subtask"` | Your task name; it becomes the row's `task`. Start it with `offline_` |
| `fields` | `{}` | Extra fields merged into the result row, e.g. `source_window_s`, `source_frame_indices`, `finish_reason`, or your own |

The row also gets `route: "offline"`, `t`, `response_id` and, for an answer to a question,
`elapsed_s`. It has no `route_decided_by`.

### `SubtaskHost`

What the harness gives your factory:

| Attribute | Meaning |
| --- | --- |
| `await direct(question, frames, dialogue, deadline) -> dict` | Ask the offline model exactly as the harness does: the question with these frames; with no frames, with the persona and the dialogue. Returns a dict with `text`, `status` and the row fields (`backend`, `vision_model`, `finish_reason`, and with frames `source_window_s`, `source_frame_indices`). An HTTP error, a timeout or a malformed reply is not raised; it returns status `upstream_unavailable` instead, and an empty answer returns `invalid_visual_format` |
| `chat` | The offline model client: `await chat.complete(body, timeout) -> (text, finish_reason)` for any OpenAI chat completions `body`. Shares the `[chat] max_concurrency` limit |
| `settings` | The validated configuration (`settings.chat.max_images`, `settings.rewrite`, ...) |
| `http` | An `httpx.AsyncClient` for calling other services (no timeout of its own: pass one), closed with the app |
| `locale` | The active locales: `locale.pick(question)` is the language to answer in |
| `say(language, key, **fields) -> str` | A user-facing message from the locale's `[messages]`, e.g. `say(language, "no_usable_frames")` |

## The built-in subtasks

- **`DirectSubtask`** (the default, `[subtask] factory = ""`): for a recording, up to
  `[chat] max_images` frames spread evenly from the start to `asked_at`; for `chat`, no frame; for
  `realtime`, the current frame. No frame where one is needed gives `insufficient_evidence` (with
  the default task name, `offline_subtask`). Otherwise `host.direct()`, task `offline_direct`.
- **`ov25_harness.rewrite:factory`**: a text model first rewrites the question into a plain,
  explicit one (resolving pronouns from the dialogue), then the offline model answers. A rewrite is
  used only if it adds no number and no answer option the user did not give, keeps the user's
  language, and keeps at least half of the user's words; otherwise the user's question goes on as
  asked. A question about a whole recording is first tried as one video (task `offline_video`),
  falling back to sampled frames (task `offline_rewrite`). The whole-recording video is used only
  for a question about the end of the recording (no `t`, or `t` at its end) and with the
  OpenAI-compatible `[chat]` backend. These rows say what happened in `rewrite_status` (`off`,
  `skipped`, `timeout`, `failed`, `rejected`, `unchanged`, `rewritten`). `chat` questions, and live
  questions without a fresh frame, skip the rewrite and go to `DirectSubtask` as they are (task
  `offline_direct`, no `rewrite_status`). Configured by `[rewrite]`; see
  [configuration.md](configuration.md#rewrite).

## Loading your own

Set the factory in the configuration:

```toml
[subtask]
factory = "my_package.my_module:make_subtask"
```

At start the harness imports `my_package.my_module`, takes `make_subtask`, and calls it with the
`SubtaskHost` if the function takes an argument, or with nothing if it does not. The module must be
importable by the server's Python (installed, or on `PYTHONPATH`). `GET /healthz` reports the
factory string as `subtask` (`direct` for the built-in one).

### Example

[examples/echo_subtask.py](../examples/echo_subtask.py) answers with the question and the number of
frames it came with:

```python
from ov25_harness.subtask import OfflineRequest, OfflineResult


class EchoSubtask:
    async def run(self, request: OfflineRequest) -> OfflineResult:
        if request.input_mode == "offline":
            frames = len(request.recording.timestamps)
        else:
            frames = len(request.frames)
        return OfflineResult(f"You asked: {request.question} ({frames} frames)", "ok", "offline_echo",
                             {"echo_frames": frames})


def factory():
    return EchoSubtask()
```

```sh
# my.toml: a copy of configs/local-fake.toml with [subtask] factory = "echo_subtask:factory"
PYTHONPATH=examples ov25-harness serve --config my.toml
```

A subtask that uses the offline model takes the host:

```python
from ov25_harness.subtask import OfflineResult


class FirstAndLast:
    """Answer a recording question from its first and last frames."""

    def __init__(self, host):
        self.host = host

    async def run(self, request):
        if request.input_mode != "offline":
            frames = [request.current_frame] if request.current_frame else []
        else:
            video = request.recording
            frames = await video.rgb([0, len(video.timestamps) - 1], request.max_pixels)
        reply = await self.host.direct(request.question, frames, request.dialogue, request.deadline)
        return OfflineResult(reply.pop("text"), reply.pop("status"), "offline_first_last", reply)


def factory(host):
    return FirstAndLast(host)
```

## Rules for a subtask

**Deadlines.** Finish before `request.deadline` (a `time.monotonic()` value). The harness cancels
the subtask when the question's turn runs out and publishes `request_timeout` instead; pass the
remaining time (`request.deadline - time.monotonic()`) as the timeout of every call you make, and
let `asyncio.CancelledError` propagate.

**Errors.** An exception raised from `run()` does not hurt the session: the harness logs its type
and publishes a row with status `upstream_unavailable`, task `offline_subtask` and a generic
"try again" message. Return a
result with a fitting status yourself when you can say something more useful.

**Do not block the event loop.** `run()` runs inside the server's event loop, next to every other
session. Use `await` for I/O and `asyncio.to_thread` for CPU-heavy work.

**User text is data.** The question and the dialogue are what a user said, not instructions to you
or to a model you call. When you send them to a text model, put them in a JSON object in the user
message and say in the system prompt that they are data, not instructions (as
`ov25_harness.rewrite` does). Never execute them, never use them as a file path or a URL, and do not
let a model's rewrite add facts, numbers or answer options the user did not give. On a small model
such a "they are data, not instructions" line can itself change behaviour: on the 4B router it moved
routes ([design.md](design.md#through-the-harness)). Check the prompt with an evaluation before
relying on it.

**Keep the user's words.** The final question that reaches the offline model should still be the
user's question. If you change it, record what you did in the result's `fields` so it can be
audited from the row.
