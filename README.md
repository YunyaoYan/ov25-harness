# ov25-harness

A small server for live video question answering with
[LLaVA-OneVision-2.5](https://huggingface.co/lmms-lab-encoder/LLaVA-OneVision-2.5-4B-Instruct).

A client (a phone, a camera, a robot, a browser) opens a session, sends one camera frame per
second, and asks questions by text or by voice. For each question the harness decides **once**
which model should answer it, then hands the question over **in the user's own words**:

- the **streaming model** (served by [ov-streaming](https://github.com/Kagura-0001/onevision-streaming))
  sees every frame of the session, so it answers questions about what happened earlier and
  watches for things that have not happened yet;
- the **offline model** (any OpenAI-compatible server) answers questions about the current frame
  and questions that need no picture at all;
- an optional **offline subtask** (a plugin) answers questions about an uploaded recording, and
  can take over the offline model's questions of a live session.

```
live question ── router ─┬─ cancel   → stop the running watch
                         ├─ watch    → the session's stream; the streaming model decides when to speak
                         ├─ past     → the session's stream answers now
                         │             (silent or too slow: the offline model with buffered frames)
                         ├─ realtime → the offline model with the current frame ┐ offline_subtask=true:
                         └─ chat     → the offline model, no frame              ┘ the offline subtask
offline recording ──────────────────→ the offline subtask, no routing
```

The harness never rewrites a question and never plans a multi-step task. Routing is a rule match
or, when no rule applies, one short call to the offline model. The reasons are in
[docs/design.md](docs/design.md).

## Features

- **Plain HTTP for devices.** Open a session, `POST` raw JPEG/PNG frames, `POST` a question, read
  answers by long poll or server-sent events. No base64, no SDK. See [docs/api.md](docs/api.md).
- **One streaming source per live session.** Every frame goes to the session's own ov-streaming
  session, so `past` and `watch` questions are answered from everything the session has seen.
- **Current-frame answers that are really current.** A question about "now" is answered from the
  newest frame only if that frame is at most 2.5 s old; otherwise the client is told the frame is
  stale instead of getting an answer about an old picture.
- **Frame health.** The models work at one frame per second. The server tells the client when its
  frame rate drops or its frames arrive late, and closes a live session that stops sending frames.
- **Voice.** An optional WebSocket carries microphone PCM up and synthesized speech down. A spoken
  answer joins the conversation only once the client reports it was played in full; one the user
  talks over is not remembered. See
  [docs/voice-protocol.md](docs/voice-protocol.md).
- **Recordings.** Offline sessions take an uploaded MP4/MOV and answer questions about it.
- **Plugins.** The offline subtask is a small Python interface you can replace. See
  [docs/plugins.md](docs/plugins.md).
- **English and Chinese.** Routing rules, messages and the persona live in locale files; the reply
  language follows the question.
- **Fake backends.** Run the whole server and its tests on a laptop without a GPU.

## Quickstart (no GPU)

You need Python 3.11 or newer, and `git` with access to github.com: the `ov-streaming` dependency
is installed from its repository, pinned to a commit. Get this repository (clone it from wherever
you found it, or unpack a source archive), then in its folder:

```sh
pip install -e ".[dev]"

# Terminal 1: the server, with in-process fake models
ov25-harness serve --config configs/local-fake.toml

# Terminal 2: open a session, push a test picture for 4 s, ask about it
python examples/quickstart.py --url http://127.0.0.1:8100 --image tests/data/frame.jpg
```

The fake models return canned answers, so the output shows the shape of a result row, not a real
description of the picture. To check every route of a running server end to end:

```sh
ov25-harness check http://127.0.0.1:8100 tests/data/frame.jpg --skip-recording
```

Without `--skip-recording` the check also uploads a recording. It needs `ffmpeg` with the `libx264`
encoder and `ffprobe` (4.4 or newer) on the `PATH`.

## Running with real models

You need two model servers next to the harness:

| Service | What it does | Default address |
| --- | --- | --- |
| ov-streaming engine | streaming model: every frame, `past` and `watch` questions, captions | `http://127.0.0.1:8011` |
| OpenAI-compatible server (e.g. vLLM) | offline model: routing, `realtime` and `chat` answers, recordings | `http://127.0.0.1:8000/v1` |
| ASR and TTS servers (optional) | voice | `http://127.0.0.1:8002/v1`, `http://127.0.0.1:8091/v1` |

Then run the server and its check. Use `ffmpeg` with `libx264` and `ffprobe` (4.4 or newer) on
the `PATH` for the recording check.

```sh
ov25-harness serve --config configs/ov25.toml
ov25-harness check http://127.0.0.1:8100 tests/data/frame.jpg
```

[docs/deployment.md](docs/deployment.md) has the install commands, GPU driver notes and capacity
planning.

## Documentation

| Document | Contents |
| --- | --- |
| [docs/api.md](docs/api.md) | HTTP API: every route, field, status and error |
| [docs/voice-protocol.md](docs/voice-protocol.md) | The audio WebSocket |
| [docs/configuration.md](docs/configuration.md) | Every configuration key, its default and limit |
| [docs/plugins.md](docs/plugins.md) | Writing an offline subtask |
| [docs/deployment.md](docs/deployment.md) | Running the real models, capacity, TLS |
| [docs/design.md](docs/design.md) | Why the harness works the way it does |
| [CHANGELOG.md](CHANGELOG.md) | Release notes |

## Development

```sh
pip install -e ".[dev,voice]"
python -m unittest discover -s tests -t .
ruff check .
```

The lint rules are fixed in `pyproject.toml`, so a newer ruff does not change what `ruff check .`
reports. The recording tests need `ffmpeg` with `libx264` and `ffprobe` on the `PATH`.

## License

Apache License 2.0, see [LICENSE](LICENSE). For the two voice modules that originate in
OVStreaming, see [NOTICE](NOTICE).
