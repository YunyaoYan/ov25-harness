# Configuration

The server reads one TOML file:

```sh
ov25-harness serve --config configs/ov25.toml
```

- Every key is optional; a missing key takes the default below. A missing table takes all its
  defaults.
- Unknown keys are errors, and every number has a range. A bad file stops the server at start with
  the name of the offending field.
- Secrets never go in the file. Where a key is needed, the file names the environment variable
  that holds it (`api_key_env`).
- URLs must be `http` or `https` with a host, and must not contain credentials, a query or a
  fragment. A trailing `/` is removed.
- `ov25-harness serve --host HOST --port PORT` overrides `[server] host` and `port`; the result is
  validated like the file.

Two files ship with the package:

| File | Use |
| --- | --- |
| `configs/local-fake.toml` | In-process fake models; no GPU, for trying the API and running tests |
| `configs/ov25.toml` | The real models: an ov-streaming engine and an OpenAI-compatible server |

## `[server]`

| Key | Default | Limit | Meaning |
| --- | --- | --- | --- |
| `host` | `"127.0.0.1"` | | Address to listen on. Set it explicitly (e.g. `"0.0.0.0"`) to accept connections from other machines |
| `port` | `8100` | 1 to 65535 | Port to listen on |
| `max_pixels` | `331776` | 1 to 2,000,000 | Pixel budget per frame passed to the models |
| `languages` | `["en"]` | 1 to 8 distinct names out of `en`, `zh` | Locales whose routing rules are active. The first one is the default reply language; a question containing Chinese characters is answered with Chinese messages when `zh` is active |
| `log_dir` | `""` | | Empty: log to stderr only. Otherwise also write `ov25-harness.log` in this directory, rotated at midnight |
| `log_keep_days` | `14` | 1 to 3650 | Days of rotated log files kept |

## `[streaming]`: the streaming engine

| Key | Default | Limit | Meaning |
| --- | --- | --- | --- |
| `kind` | `"ov-streaming"` | `ov-streaming`, `fake` | `fake` uses an in-process stand-in |
| `url` | `"http://127.0.0.1:8011"` | | Base URL of the `ov-streaming serve` server |
| `user_streams` | `4` | 0 to 256 | Live sessions open at once. Each holds one engine session; see [deployment.md](deployment.md#capacity) for sizing it against the engine |
| `max_visual_frames` | `150` | 1 to 4096, ≤ `max_visual_frames_limit` | Frames a live session's source keeps before compaction, when the client does not ask for a value |
| `compact_to_visual_frames` | `75` | 1 to 100, ≤ `max_visual_frames` | Frames kept after a compaction, when the client asks for neither value (a client that sets only `max_visual_frames` gets half of it) |
| `max_visual_frames_limit` | `700` | 1 to 4096 | The most a client may ask for with `max_visual_frames`. Raise it only with an engine started with a long enough `--max-model-len` |
| `frame_backlog` | `8` | 1 to 64 | Frames sent to the source and not yet decided on. At this many the session's source is dropped; the session goes on without it ([api.md](api.md#the-streaming-source)) |

Without a frame limit one long session grows until it exhausts the engine and takes every other
session with it, so live sessions always compact.

## `[chat]`: the offline model

Any server that speaks the OpenAI `/v1/chat/completions` API with image inputs.

| Key | Default | Limit | Meaning |
| --- | --- | --- | --- |
| `kind` | `"openai"` | `openai`, `fake` | `fake` uses an in-process stand-in |
| `url` | `"http://127.0.0.1:8000/v1"` | | Base URL; requests go to `url + "/chat/completions"` |
| `model` | `"ov25-4b-instruct"` | not empty | The served model name |
| `max_concurrency` | `8` | 1 to 256 | Requests in flight to the offline model at once, across all sessions |
| `max_images` | `8` | 1 to 64 | Images per request. Must match the server's `--limit-mm-per-prompt` |
| `context_tokens` | `12288` | 1024 to 1,048,576 | The server's `--max-model-len`; used to fit a whole recording as one video |
| `encoder_tokens` | `11552` | 1 to 1,048,576 | The visual encoder's token limit on that server; used the same way |

## `[voice]`

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Serve the audio socket ([voice-protocol.md](voice-protocol.md)). Needs the `voice` extra |
| `asr_url` | `"http://127.0.0.1:8002/v1"` | OpenAI-compatible speech recognition (`/audio/transcriptions`) |
| `asr_model` | `"Qwen/Qwen3-ASR-1.7B"` | ASR model name |
| `tts_url` | `"http://127.0.0.1:8091/v1"` | OpenAI-compatible speech synthesis (`/audio/speech`) |
| `tts_model` | `"Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"` | TTS model name |
| `tts_voice` | `"vivian"` | TTS voice |
| `tts_rate` | `24000` | Sample rate the TTS server returns, 8000 to 48000 |

## `[subtask]`

| Key | Default | Meaning |
| --- | --- | --- |
| `factory` | `""` | Empty: the built-in `DirectSubtask`. Otherwise `"package.module:function"`, e.g. `"ov25_harness.rewrite:factory"`. See [plugins.md](plugins.md). `GET /healthz` reports it as `subtask` (`direct` when empty) |

## `[rewrite]`

Used only by the `ov25_harness.rewrite` subtask. It sends question text (never pictures) to this
service; nothing is sent unless `enabled` is true and `url` is set. `enabled = true` without
`url` and `model` is a configuration error.

| Key | Default | Limit | Meaning |
| --- | --- | --- | --- |
| `enabled` | `false` | | Turn the rewrite on |
| `url` | `""` | empty or an http(s) URL | Full OpenAI-compatible chat completions URL of the text model |
| `model` | `""` | | Model name |
| `api_key_env` | `""` | an environment variable name | Name of the environment variable holding the API key. Empty: no `Authorization` header. If it is set here but the variable is missing, the rewrite is turned off with a warning in the log |
| `timeout_s` | `3` | above 0, at most 30 | Longest a rewrite may take, and never more than a quarter of the time left for the question; on timeout the question goes on as asked |

## `[media]`

| Key | Default | Limit | Meaning |
| --- | --- | --- | --- |
| `ffmpeg` | `"ffmpeg"` | | ffmpeg executable, for decoding frames from recordings (4.4 or newer) |
| `ffprobe` | `"ffprobe"` | | ffprobe executable, for inspecting recordings |
| `max_upload_bytes` | `134217728` (128 MiB) | 1 to 1 GiB | Largest recording upload |
| `max_recording_s` | `1800` | above 0, at most 3600 | Longest recording, in seconds |
| `max_offline_sessions` | `4` | 0 to 64 | Offline sessions open at once |

## `[policy]`

| Key | Default | Limit | Meaning |
| --- | --- | --- | --- |
| `live_turn_s` | `14` | above 0, at most 120 | Time budget of one live question, from arrival to answer |
| `offline_turn_s` | `145` | above 0, at most 600 | Time budget of one question about a recording |
| `route_timeout_s` | `2` | above 0, at most 30 | Longest the router's model call may take; then the question is treated as `realtime` |
| `current_frame_age_s` | `2.5` | above 0, at most 60 | How old the newest frame may be, by timestamp and by arrival, to answer a question about "now" |
| `live_idle_s` | `10` | above 0, at most 3600 | A live session without an accepted frame for this long is closed. `ov25-harness check` assumes 10; pass it `--live-idle-s` otherwise |
| `offline_idle_s` | `600` | above 0, at most 86400 | An offline session without an upload or a question for this long is closed |
| `max_frame_bytes` | `2097152` (2 MiB) | 1 to 8 MiB | Largest frame |
| `buffer_frames` | `90` | 1 to 1000 | Frames each live session keeps in memory for current-frame answers, the `past` fallback and the subtask |
| `buffer_bytes` | `12582912` (12 MiB) | 1 to 256 MiB | Bytes each live session keeps in that buffer |
| `dialogue_turns` | `6` | 0 to 64 | Question and answer pairs the offline model sees for `chat` questions |

## A complete file

```toml
[server]
host = "127.0.0.1"
port = 8100
max_pixels = 331776
languages = ["en", "zh"]
log_dir = ""
log_keep_days = 14

[streaming]
kind = "ov-streaming"
url = "http://127.0.0.1:8011"
user_streams = 4
max_visual_frames = 150
compact_to_visual_frames = 75
max_visual_frames_limit = 700
frame_backlog = 8

[chat]
kind = "openai"
url = "http://127.0.0.1:8000/v1"
model = "ov25-4b-instruct"
max_concurrency = 8
max_images = 8
context_tokens = 12288
encoder_tokens = 11552

[voice]
enabled = false
asr_url = "http://127.0.0.1:8002/v1"
asr_model = "Qwen/Qwen3-ASR-1.7B"
tts_url = "http://127.0.0.1:8091/v1"
tts_model = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
tts_voice = "vivian"
tts_rate = 24000

[subtask]
factory = ""

[rewrite]
enabled = false
url = ""
model = ""
api_key_env = ""
timeout_s = 3

[media]
ffmpeg = "ffmpeg"
ffprobe = "ffprobe"
max_upload_bytes = 134217728
max_recording_s = 1800
max_offline_sessions = 4

[policy]
live_turn_s = 14
offline_turn_s = 145
route_timeout_s = 2
current_frame_age_s = 2.5
live_idle_s = 10
offline_idle_s = 600
max_frame_bytes = 2097152
buffer_frames = 90
buffer_bytes = 12582912
dialogue_turns = 6
```

## Locales

Routing rules, the router's examples, the persona for `chat` answers, every message a user can see,
and the sample questions used by `ov25-harness check` live in `src/ov25_harness/locales/en.toml` and
`zh.toml`. Both files have the same keys:

| Table | Contents |
| --- | --- |
| `[rules]` | `stop` (stop phrase, full match), `past_prefix` (past marker at the start), `alert` (watch phrases with a named group `target`), `alert_compound` (words that make a watch target more than one visible thing, so the router decides instead) |
| `[router]` | Example phrases per route, added to the router prompt, and the `separator` they are joined with. Questions longer than 200 characters skip the `alert` rules and go to the router |
| `[persona]` | `chat`: who the assistant is, for questions without a picture |
| `[messages]` | Every user-facing message (stale frame, stream unavailable, watch accepted, frame-health warnings, ...) |
| `[check]` | Sample questions for `ov25-harness check --language` |

The rules of every language in `[server] languages` apply to every question; the messages follow
the language of the question.
