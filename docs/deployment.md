# Deployment

This page runs the harness against real models on a Linux server with NVIDIA GPUs:

```
client ──HTTP──▶ ov25-harness :8100 ─┬─▶ ov-streaming engine :8011   (streaming model)
                                     ├─▶ OpenAI-compatible server :8000  (offline model)
                                     └─▶ ASR :8002 / TTS :8091  (optional, voice)
```

All model servers listen on `127.0.0.1` by default and only the harness talks to them. Give the
streaming engine and the offline model a GPU each (`CUDA_VISIBLE_DEVICES`).

The real-model checks below were run on the November 26, 2026 pre-release build. The current
GitHub Actions workflow tests the harness with fake backends.

- **Verified:** the streaming engine on a server with NVIDIA driver 550 (CUDA 12.4 class), with the
  CUDA 12.9 packages under [Drivers older than 580](#drivers-older-than-580), served by
  `ov-streaming serve` with `--renderer ov21` and with `--renderer ov21-window`,
  `--max-model-len 131072` and `max_pixels` 331776. The engine-only probe in
  [design.md](design.md#the-engine-alone) talked to it directly.
- **Verified:** the offline model command in [section 2](#2-the-offline-model), on the same driver
  with the CUDA 12.9 builds (vllm 0.25.1+cu129, torch 2.11.0+cu129, transformers 5.9.0,
  ov-streaming 0.2.0). The harness's own requests were sent to it: routing (JSON-schema output with
  thinking off), answers with pictures, answers without one (persona and dialogue), and a whole
  recording as one video with 32 frames.
- **Verified:** the harness against real models end to end. `ov25-harness check --voice` passed
  every check in English and in Chinese against the streaming engine (`--renderer ov21`), the
  offline model on vLLM (that run's server had `--max-model-len 32768` and
  `--limit-mm-per-prompt '{"image":8,"video":1}'`), and Qwen3-ASR-1.7B and
  Qwen3-TTS-12Hz-1.7B-CustomVoice for voice, at 16 kHz and 24 kHz. The same setup ran the benchmark
  in [design.md](design.md#through-the-harness).
- Also verified: the harness's unit tests, `ov25-harness check` against the fake backends
  (`configs/local-fake.toml`), and a clean install from this repository on Python 3.12.
- The CUDA 13 install (driver 580 or newer), the offline model without `--enforce-eager` (with CUDA
  graphs), and the nginx example follow the packages' own documentation and still need validation.

## 1. The streaming engine

The streaming model is served by the public
[ov-streaming](https://github.com/Kagura-0001/onevision-streaming) package on official vLLM 0.25.1.
Use its own virtual environment. The commit pinned below is the one the harness's own `ov-streaming`
dependency is pinned to, and the one verified. On driver 580 or newer (recipe pending validation):

```sh
python -m venv ~/venvs/ov-streaming && . ~/venvs/ov-streaming/bin/activate
pip install "vllm==0.25.1" "transformers>=5.7.0,<5.10" "qwen-vl-utils>=0.0.14"
pip install "ov-streaming[serve] @ git+https://github.com/Kagura-0001/onevision-streaming.git@777c0ab3e923a1852d9b58af6dfb4f403d755589"
```

The example starts the engine on GPU 0. Adjust the device indices for your machine.

```sh
CUDA_VISIBLE_DEVICES=0 ov-streaming serve lmms-lab-encoder/LLaVA-OneVision-2.5-4B-Instruct \
  --renderer ov21 \
  --max-model-len 131072 \
  --max-num-batched-tokens 1024 \
  --enforce-eager \
  --mm-processor-kwargs '{"max_pixels":331776}' \
  --host 127.0.0.1 --port 8011
```

- The verified settings are `--renderer`, `--max-model-len 131072` and `max_pixels` 331776;
  `--max-num-batched-tokens 1024` and `--enforce-eager` are suggested, not part of what was
  verified.
- It listens on `127.0.0.1:8011` by default, which is the harness's default `[streaming] url`.
- `--renderer` picks the prompt format: `ov21`, `ov21-window`, `ov25`, `ov25-window` or `univia`.
  ov-streaming's own default is `ov25-window`, so pass the renderer explicitly (see
  [Which renderer](#which-renderer)).
- `max_pixels` should match the harness's `[server] max_pixels`.
- It accepts the usual vLLM engine flags (`ov-streaming serve --help=all`); see
  [Capacity](#capacity) for `--max-num-seqs`.

### Which renderer

Use `--renderer ov21`. It and `ov21-window` are the two renderers measured with the pre-release build
([design.md](design.md#the-engine-alone), 120 StreamingBench real-time questions, the "past subset"
being the 70 a forced baseline sent to the stream). On that sample their scores differ by at most
one question:

| | `ov21` | `ov21-window` |
| --- | --- | --- |
| Questions answered at once, of 120 | 118 | 118 |
| Correct after the clock prefix is removed, of 120 | 102 | 101 |
| Correct after the clock prefix is removed, past subset of 70 | 61 | 60 |
| Median time from question to answer | 1.514 s | 1.482 s |

The two agree on 117 of the 120 answers. `ov21` is recommended because it scored one question
higher on both counts and is the renderer the verified engine was left serving; `ov21-window` is an
equally reasonable choice on these numbers, since one 120-question sample does not separate them.
`ov25`, `ov25-window` and `univia` have not been measured with this harness.

### Drivers older than 580

pip's default `torch` 2.11.0 and `vllm` 0.25.1 wheels are built for CUDA 13, which needs driver 580
or newer. On an older driver they fail with `The NVIDIA driver on your system is too old`. Install
the CUDA 12.9 builds instead, in this order. The vLLM wheel is an asset of the
[official v0.25.1 release](https://github.com/vllm-project/vllm/releases/tag/v0.25.1).

```sh
pip install torch==2.11.0 torchvision==0.26.0 torchaudio==2.11.0 \
  --index-url https://download.pytorch.org/whl/cu129
pip install "transformers>=5.7.0,<5.10" "qwen-vl-utils>=0.0.14"
pip install "ov-streaming[serve] @ git+https://github.com/Kagura-0001/onevision-streaming.git@777c0ab3e923a1852d9b58af6dfb4f403d755589"
pip install --no-deps \
  "https://github.com/vllm-project/vllm/releases/download/v0.25.1/vllm-0.25.1%2Bcu129-cp38-abi3-manylinux_2_28_x86_64.whl"
```

`vllm` goes in last with `--no-deps` so that it does not pull the CUDA 13 `torch` back in. If any of
vLLM's own dependencies are still missing afterwards, `pip check` lists them; install them without
replacing `torch` (this last step is advice, not part of the verified install).

Also install a `torchcodec` build that matches torch 2.11 and CUDA 12.9: the CUDA 13 `torchcodec`
fails to load `libtorchcodec_image.so` on these drivers.

## 2. The offline model

Any OpenAI-compatible server for
[LLaVA-OneVision-2.5-4B-Instruct](https://huggingface.co/lmms-lab-encoder/LLaVA-OneVision-2.5-4B-Instruct)
works. With official vLLM, run it from the streaming engine's environment of section 1 (including
the CUDA 12.9 builds on drivers older than 580), as a second process on another GPU:

```sh
. ~/venvs/ov-streaming/bin/activate
CUDA_VISIBLE_DEVICES=1 vllm serve lmms-lab-encoder/LLaVA-OneVision-2.5-4B-Instruct \
  --served-model-name ov25-4b-instruct \
  --trust-remote-code \
  --hf-overrides '{"model_type":"llava_onevision2_qwen35"}' \
  --max-model-len 12288 \
  --limit-mm-per-prompt '{"image":8,"video":1}' \
  --gpu-memory-utilization 0.85 \
  --enforce-eager \
  --host 127.0.0.1 --port 8000
```

This command was verified with a local copy of the checkpoint in place of the repository id, and on
another port. Three things in it are needed:

- **The `ov-streaming` package.** It registers a vLLM plugin for the LLaVA-OneVision-2.5
  architecture. Plain vLLM 0.25.1 without it stops with `Model architectures
  ['LlavaOnevision2_5ForConditionalGeneration'] are not supported for now`.
- **`--hf-overrides '{"model_type":"llava_onevision2_qwen35"}'`.** Without it vLLM 0.25.1 stops at
  start with `The text_config extracted from the model config does not have 'num_attention_heads'
  attribute`. With this model type the plugin's configuration class builds a proper Qwen3.5 text
  configuration; the architecture stays the same. Only `model_type` is needed:
  `{"model_type":"llava_onevision2_5"}`, or an override of `architectures` alone, still fail. (The
  two-key form the streaming engine's `ov21` renderer uses, `model_type` plus `architectures`
  `["LlavaOnevision2Qwen35ForConditionalGeneration"]`, passes the same configuration check, but was
  not served.)
- **The activated environment**, not just its `vllm` called by its full path. flashinfer compiles
  its sampling kernels when the engine warms up and runs `ninja`, which pip installs in the
  environment's `bin` directory; without it on `PATH` the engine core stops with
  `FileNotFoundError: ... 'ninja'`. No CUDA toolkit (`nvcc`) is needed. The first start writes
  flashinfer's compile cache under `~/.cache/flashinfer`.

With `--gpu-memory-utilization 0.85` on an 80 GB GPU the server used about 69.6 GB (weights
9.94 GiB, KV cache 53.78 GiB) and was ready about 30 s after it was started with the weights in the
page cache. `--limit-mm-per-prompt` is enforced exactly: a request with 9 images gets HTTP 400
`At most 8 image(s) may be provided in one prompt`, so `[chat] max_images` must not exceed it.

The streaming engine's `ov21` renderer also sets `mamba_ssm_cache_dtype` float32,
`mamba_cache_mode` none, `enable_prefix_caching` False and `enable_mm_embeds`. The command above
leaves vLLM's defaults for these; every request shape the harness sends worked with them, but answer
quality with and without `--mamba-ssm-cache-dtype float32` has not been compared.

Keep the harness's `[chat]` table in step with this server:

| Server flag | `[chat]` key |
| --- | --- |
| `--served-model-name ov25-4b-instruct` | `model = "ov25-4b-instruct"` |
| `--host 127.0.0.1 --port 8000` | `url = "http://127.0.0.1:8000/v1"` |
| `--max-model-len 12288` | `context_tokens = 12288` |
| `--limit-mm-per-prompt '{"image":8,...}'` | `max_images = 8` |

The harness uses this model for routing (with structured JSON output), for `realtime` and `chat`
answers, for the `past` fallback and for recordings.

## 3. Voice (optional)

Voice needs an OpenAI-compatible speech recognition server (`POST /audio/transcriptions`, e.g.
Qwen3-ASR) and speech synthesis server (`POST /audio/speech` returning raw PCM, e.g. Qwen3-TTS), the
`voice` extra (`pip install "ov25-harness[voice]"`), and `[voice] enabled = true` with their URLs.
Verified with Qwen3-ASR-1.7B and Qwen3-TTS-12Hz-1.7B-CustomVoice (`ov25-harness check --voice`, at
16 kHz and 24 kHz). How to serve those two is up to their own documentation.

## 4. The harness

In its own environment (Python 3.11 or newer), with `ffmpeg` and `ffprobe` on the `PATH` if you
accept recordings (4.4 or newer; 4.4 and 8.1 are tested). Installing needs `git` and access to
github.com, because the `ov-streaming` dependency is pinned to a commit of its repository. For the
same reason the package cannot be published on PyPI. Run these commands from the repository root.

```sh
python -m pip install .
ov25-harness serve --config configs/ov25.toml
```

Then check every route end to end from the same machine:

```sh
ov25-harness check http://127.0.0.1:8100 tests/data/frame.jpg
```

The recording check uses an FFmpeg build with the `libx264` encoder to create its test video.
Run `ffmpeg -hide_banner -encoders` to check for it, or use `--skip-recording` to run the live checks.
Add `--voice` to check speech at 16 kHz and 24 kHz, and `--language zh` to ask the sample questions
in Chinese. Set `--live-idle-s` to match the service's `[policy] live_idle_s` if you changed the
default of 10 seconds. The command opens and closes its own sessions, prints one line per check,
and exits with status 1 if a check failed. Against the real models it takes about a minute.

## Capacity

**One engine session per live session.** Each live harness session opens one session on the
streaming engine, so `[streaming] user_streams` is the number of live sessions the harness serves
at once. Offline sessions do not use the engine; `[media] max_offline_sessions` limits them.

**Engine request slots.** The engine refuses a new session when the request slots its sessions
reserve would exceed its `--max-num-seqs`. A session reserves one slot per response branch plus two
(its source, and the source a compaction opens next to it):

| Live session | Engine `max_responses` | Slots held |
| --- | --- | --- |
| `mode: "qa"` | 1 | 3 |
| `mode: "caption"` | 3 | 5 |

So size `user_streams × 5 ≤ --max-num-seqs` if any client may open caption sessions, or
`user_streams × 3 ≤ --max-num-seqs` if all are QA. Count other clients of the same engine too. When
the engine is full, opening a live session returns
`503 no streaming capacity for a live session`. GPU memory is the other limit: every session keeps
up to `max_visual_frames` frames in the engine's KV cache, so measure with your longest sessions
before raising `user_streams`.

**Offline model.** `[chat] max_concurrency` (8) caps requests in flight from the harness to the
offline model across all sessions.

**Memory in the harness.** Each live session buffers up to `[policy] buffer_frames` (90) frames and
`[policy] buffer_bytes` (12 MiB).

## Network exposure and TLS

The harness speaks plain HTTP and has no user authentication: anyone who can reach it can open
sessions, and a session id is all it takes to use a session. Keep it on `127.0.0.1` (the default)
or a private network, and put a reverse proxy in front of it for TLS and access control. The proxy
must:

- pass WebSocket upgrades on `/v1/sessions/*/audio`;
- not buffer `/v1/sessions/*/events` (server-sent events) and allow long reads (results long polls
  wait up to 60 s);
- accept request bodies as large as `[media] max_upload_bytes` on `/v1/sessions/*/video`.

Example nginx configuration (pending validation):

```nginx
location / {
    proxy_pass http://127.0.0.1:8100;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;   # map $http_upgrade in the http block
    proxy_buffering off;
    proxy_read_timeout 120s;
    client_max_body_size 128m;
}
```

## Operations

- **Sessions are in memory only.** Restarting the harness drops every session; clients must open
  new ones. Restarting the streaming engine breaks the streaming source of every live session (they
  go on answering `realtime` and `chat`, and report `stream_unavailable` for `past` and `watch`).
- **Logs.** The harness logs through Python `logging` (logger names `ov25_harness.*`) to stderr, and
  to a daily-rotated file in `[server] log_dir` when it is set. INFO logs can contain speech
  transcripts, answers and captions. Run it under a process supervisor (systemd, a container
  runtime) that restarts it.
- **Health.** `GET /healthz` returns `{"ok": true, ...}` with the session count; use it for liveness
  checks. `ov25-harness check` is the end-to-end test after a deployment.
