# Changelog

All notable changes to this project are recorded here. The project follows
[Semantic Versioning](https://semver.org/).

## Unreleased

- Recordings reach the offline model as a video: frames at a steady rate, each behind its
  `<T.T seconds>` tag as the model's chat template renders video frames, so answers can say when
  something happens. Before, a question sent `[chat] max_images` (8) frames as separate pictures with
  no times. A recording is decoded once, right after the upload, and its frames are shared by every
  question of the session; closing the session stops a decode still running.
- The frame rate and size of a recording follow its length and resolution: one frame a second at
  about 480 × 270 up to 256 s; at least 256 frames, at most one every two seconds, at about 200,000
  pixels for longer recordings; never larger than the recording, always within `[chat]
  context_tokens` and `max_images` ([design.md](docs/design.md#reading-a-recording)).
- An offline-model answer cut off at `max_tokens` (`finish_reason: "length"`) ends at its last full
  sentence, or where a list it keeps repeating first comes round again, with "etc." in the answer's
  language, instead of mid-word. New locale message `list_continues`.
- Rows of answers from more than 8 frames carry `video_frames` instead of every frame index.
- `[chat]` defaults follow the documented server: `context_tokens` 102400 (was 12288), `max_images`
  2048 (was 8, limit raised to 4096). The `past` fallback keeps its 8 buffered frames under the new
  `[chat] past_images`.
- docs/deployment.md: the offline model runs with CUDA graphs (no `--enforce-eager`), which decoded
  4.5 times faster with the same answers, `--max-model-len 102400` and
  `--limit-mm-per-prompt '{"image":2048,"video":1}'`.

## 0.1.0 - 2026-11-26

Initial release.

- HTTP API version 1 under `/v1/sessions`: open a live or offline session, push frames at one per
  second, upload a recording, ask questions, read results by long poll or server-sent events, stop a
  watch, read the session log, close.
- Each live question is routed once (rules first, then one call to the offline model) to one of
  five routes: `realtime`, `chat`, `past`, `watch`, `cancel`. The question reaches the model in the
  user's own words.
- `past` questions are answered by the session's streaming source; when the streaming model stays
  silent or runs out of time, the offline model answers from frames buffered by the harness.
- Uses the public `ov-streaming` package as the streaming engine client.
- Offline subtask plugin interface (`OfflineRequest`, `OfflineResult`, `SubtaskHost`) with a direct
  implementation and an optional question-rewrite implementation.
- Optional voice WebSocket: microphone PCM in, synthesized speech out. A spoken answer is remembered
  only once the client reports it was played in full; an interrupted one is not remembered.
- The streaming model's clock prefix (`At 12 seconds: ...`) is removed from `past` answers and watch
  reports.
- One TOML configuration file, validated at start; English and Chinese locales.
- Fake streaming and chat backends for running and testing without a GPU.
- `ov25-harness check`, an end-to-end check of a running service; `--live-idle-s` tells it a
  service's own idle limit.
- Request limits a client can meet: a frame's or question's `t` is at most 10^10 s, on HTTP and on
  the voice socket alike, and `t * fps` must be below 2^63 so `frame_index` fits 64 bits (`422`).
  An utterance is limited to 30 s of speech with server VAD as with client VAD; the second kept
  from before the detection and the silence that ends it do not count. A malformed number on the
  voice socket (`speech.end` `t`, `cancel` `played_ms`, a `hello` `t` out of range) gets an error
  message and the socket stays open.
- When a `past` stream answer times out, the fallback reads the buffer from where the source window
  started when the engine took the question, so frames compacted while the answer was being written
  are still covered.
- The idle-close reason states `live_idle_s` as configured, fractions included (`0.5 s`).
- Recordings need `ffmpeg` and `ffprobe` 4.4 or newer; 4.4 and 8.1 are tested.
- In the November 26 end-to-end run, the pre-release build scored 100/120 StreamingBench
  real-time, 9/30 sequential QA and 17/40 proactive output without forced answers. It matched the earlier
  forced harness on every set, with the same routes on the real-time and sequential-QA sets and the
  same route counts on proactive output. Median real-time latency was 0.87 s against 0.90 s.
  See [the benchmark setup](docs/design.md#through-the-harness).

### Known issues

- Voice: if the speech synthesis server fails partway through an answer, later answers are not
  spoken until the user next speaks.
- The `cancelled` status is reserved and not published; a question replaced by a newer one gets no
  row.
