# HTTP API (version 1)

This is the interface a client uses: open a session, send frames, ask questions, read answers,
close. Everything is plain HTTP and JSON except frames and recordings, which are sent as raw bytes.
The optional voice socket is described separately in [voice-protocol.md](voice-protocol.md).

- All session routes live under `/v1/sessions`.
- Every HTTP response carries the header `x-ov25-harness-version: <version>`, e.g. `0.1.0`, an
  unexpected `500 Internal Server Error` included.
- Errors are HTTP status codes with a JSON body `{"detail": "<reason>"}`. Request bodies that fail
  validation get `422` with FastAPI's usual list of field errors in `detail`.
- Times called `t` are **session seconds**: seconds since the client's own session start, on the
  client's clock (see [Frames](#frames)).

## Contents

- [Routes](#routes)
- [Sessions](#sessions)
- [Frames](#frames)
- [Questions](#questions)
- [Results](#results)
- [Result rows](#result-rows)
- [Routes, tasks and backends](#routes-tasks-and-backends)
- [Statuses](#statuses)
- [Offline recordings](#offline-recordings)
- [Session logs](#session-logs)
- [Errors](#errors)
- [Worked example with curl](#worked-example-with-curl)

## Routes

| Method and path | Purpose |
| --- | --- |
| `GET /healthz` | Service status |
| `POST /v1/sessions` | Open a session |
| `POST /v1/sessions/{sid}/frame?t=<seconds>` | Send one frame (raw JPEG or PNG body) to a live session |
| `POST /v1/sessions/{sid}/video` | Upload a recording (raw MP4 or MOV body) to an offline session |
| `POST /v1/sessions/{sid}/text` | Ask a question |
| `GET /v1/sessions/{sid}/results?after=<seq>&wait=<seconds>` | Read result rows, optionally waiting for new ones |
| `GET /v1/sessions/{sid}/events` | The same rows as server-sent events |
| `WS /v1/sessions/{sid}/audio` | Voice: microphone in, speech out ([voice-protocol.md](voice-protocol.md)) |
| `DELETE /v1/sessions/{sid}/watch` | Stop the running watch |
| `GET /v1/sessions/{sid}/logs` | This session's server-side log (header `X-Session-Log-Token`) |
| `DELETE /v1/sessions/{sid}` | Close the session |

### `GET /healthz`

```json
{
  "ok": true,
  "version": "0.1.0",
  "api_version": 1,
  "sessions": 2,
  "closing_sessions": 0,
  "voice": false,
  "languages": ["en"],
  "streaming": "ov-streaming",
  "chat": "openai",
  "subtask": "direct"
}
```

| Field | Meaning |
| --- | --- |
| `sessions` | Sessions open now, live and offline |
| `closing_sessions` | Sessions being closed (their resources are not free yet) |
| `voice` | Whether the audio socket has its speech backends (`[voice] enabled`) |
| `languages` | Active locales, from `[server] languages` |
| `streaming`, `chat` | Backend kinds from the configuration: `ov-streaming` or `fake`, `openai` or `fake` |
| `subtask` | The offline subtask: `direct` for the built-in one, else the `[subtask] factory` string, e.g. `ov25_harness.rewrite:factory` |

## Sessions

There are two kinds of session:

- **live** (`input_mode: "live"`, the default): the client sends a frame every second and asks
  questions about what the camera sees, saw, or will see. Each live session holds one session on the
  streaming engine.
- **offline** (`input_mode: "offline"`): the client uploads one recording and asks questions about
  it. No frames, no streaming engine.

### `POST /v1/sessions`

Request body (JSON, every field optional; an empty body opens a live QA session). Unknown fields
are refused with `422`, including the options earlier versions took (`caption_bias`, `sampling`,
`sample_budget`, `max_responses`):

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `input_mode` | `"live"` \| `"offline"` | `"live"` | Kind of session |
| `mode` | `"qa"` \| `"caption"` | `"qa"` | Live only (ignored for offline sessions). `caption` also asks the streaming model to write captions on its own; questions still work |
| `fps` | number > 0 | `30` | The camera's own frame rate. It does not change how often you send frames (always one per second); it tells the streaming model how to place each frame in time (`frame_index = floor(t * fps)`). Send your camera's real rate |
| `voice` | bool | `false` | Allow the audio socket on this session, and remember spoken answers only once the user has heard them |
| `native_events` | bool | `false` | Live only. Also publish the streaming engine's own events as `native_event` rows (for debugging) |
| `max_visual_frames` | int ≥ 1 | `[streaming] max_visual_frames` (150) | Live only. Frames the streaming source keeps before it is compacted. At most `[streaming] max_visual_frames_limit` (700) |
| `compact_to_visual_frames` | int ≥ 1 | `[streaming] compact_to_visual_frames` (75) | Live only. Frames kept after a compaction. Must be at most `max_visual_frames` and at most 100. When you set `max_visual_frames` but not this, it is half of `max_visual_frames` (at least 1, at most 100) |
| `offline_subtask` | bool | `false` | Live only. Send `realtime` and `chat` questions to the offline subtask instead of straight to the offline model ([plugins.md](plugins.md)) |

Reply `201` for a **live** session:

```json
{
  "session_id": "3f0c...",
  "api_version": 1,
  "input_mode": "live",
  "mode": "qa",
  "fps": 30.0,
  "max_visual_frames": 150,
  "compact_to_visual_frames": 75,
  "frame_interval_s": 1,
  "native_events": false,
  "voice": false,
  "frame_url": "/v1/sessions/3f0c.../frame",
  "text_url": "/v1/sessions/3f0c.../text",
  "results_url": "/v1/sessions/3f0c.../results",
  "events_url": "/v1/sessions/3f0c.../events",
  "watch_url": "/v1/sessions/3f0c.../watch",
  "logs_url": "/v1/sessions/3f0c.../logs",
  "logs_token": "kq1...",
  "audio_url": "/v1/sessions/3f0c.../audio"
}
```

`logs_url` and `logs_token` are present for `qa` sessions only, and the reply then carries
`Cache-Control: no-store`. `audio_url` is present only with `voice: true`. `frame_interval_s` is
always `1`: send one frame a second.

Reply `201` for an **offline** session:

```json
{
  "session_id": "9a21...",
  "api_version": 1,
  "input_mode": "offline",
  "video_url": "/v1/sessions/9a21.../video",
  "text_url": "/v1/sessions/9a21.../text",
  "results_url": "/v1/sessions/9a21.../results",
  "events_url": "/v1/sessions/9a21.../events"
}
```

(plus `audio_url` with `voice: true`).

Errors: `503 no streaming capacity for a live session` when `[streaming] user_streams` live sessions
are already open or the streaming engine refused a new session; `503 offline session limit reached`
when `[media] max_offline_sessions` offline sessions are open; `422` when the frame limits are out
of range or the body has an unknown field.

### `DELETE /v1/sessions/{sid}`

Closes the session, frees its streaming source and its buffered frames.

```json
{"session_id": "3f0c...", "closed": true, "results": 12, "frames": 61}
```

`results` is the number of result rows still held, `frames` the number of frames the session
accepted. `503 session cleanup unconfirmed` means some part of the session did not shut down
cleanly; the session is still removed.

### When the server closes a session

- A **live** session that receives no accepted frame for `[policy] live_idle_s` (10 s) is closed.
  Questions do not count; only frames keep a live session open. Before closing, it publishes a
  `closed` row with `reason: "no_frames"`. Afterwards requests to it get `404` (`409` while the
  close is still running) with `detail` `session closed: no frame for 10 s`, so a client that comes
  back learns why. The exceptions: `DELETE /v1/sessions/{sid}` during the close waits for it and
  answers `200`; `/logs` answers `404 session log unavailable` as for any closed session; the audio
  socket closes with code 4404 and the same reason.
- An **offline** session with no upload and no question for `[policy] offline_idle_s` (600 s) is
  closed silently.
- Sessions live in memory only. Restarting the server drops all of them.

## Frames

`POST /v1/sessions/{sid}/frame?t=<seconds>` with the raw image bytes as the body (any
`Content-Type`; the bytes are checked, not the header).

Rules:

- **JPEG or PNG.** The body must start with the JPEG marker `FF D8 FF` or the full 8-byte PNG
  signature. Anything else is `415`.
- **At most `[policy] max_frame_bytes`** (2 MiB), uploaded within 3 s.
- **`t` is the capture time** of this frame in seconds since the session started, by the client's
  own clock: `0` for the first frame, then the real elapsed time. Not a counter, and not the time
  the request is sent. The streaming model reads this time, and current-frame answers compare it
  with the question's time. It is at most 10^10 (a larger one is a `422` validation error), and
  `t * fps` must be below 2^63, so that `frame_index` fits a 64-bit integer (only an extreme `fps`
  reaches that).
- **One frame per second.** The models are trained on one frame a second. The server accepts one
  frame per whole second of `t` (`floor(t)` must grow) and at least 0.5 s apart in arrival time.
  Extra frames are not errors; they get `accepted: false` with a reason and are dropped.
  Send a frame every second, even when nothing changes: a live session without frames is closed.

Reply `200`, accepted:

```json
{"accepted": true, "t": 12.0, "bytes": 48213, "cursor": 7,
 "health": {"status": "ok", "frames_10s": 10, "delay_s": 0.04}}
```

Reply `200`, not accepted:

```json
{"accepted": false, "reason": "live_1hz_sampling", "t": 12.4, "cursor": 7,
 "health": {"status": "ok", "frames_10s": 10, "delay_s": 0.04}}
```

| `reason` | Meaning |
| --- | --- |
| `live_1hz_sampling` | A frame for this second (`floor(t)`) or a later one was already accepted |
| `live_wall_rate` | The previous accepted frame arrived less than 0.5 s ago |
| `caption_backlog` | The streaming source has `[streaming] frame_backlog` frames it has not decided on yet and could not be dropped. Normally a source that falls this far behind is dropped at once and the frame is accepted without it (see below) |

`cursor` is the newest result `seq`, so a client can tell whether there is something to read.

### Frame health

`health` in every frame reply says how the session's frames are doing:

| `health.status` | When |
| --- | --- |
| `ok` | One frame a second, on time |
| `low_frame_rate` | Fewer than 8 frames accepted in the last 10 s (judged only once the oldest remembered frame is 10 s old) |
| `network_delay` | The newest frame arrived more than 2 s later, relative to its own `t`, than the session's fastest frame did. Checked before `low_frame_rate` |

`frames_10s` counts accepted frames in the last 10 s and `delay_s` is the newest frame's delay. When
the status leaves `ok` or changes to the other bad status, the session also publishes one `warning`
row (at most one every 10 s), whose `text` tells the user what is wrong in the language of their
last question. The server also checks once a second between frames, so a warning can arrive while
frames have stopped.

### The streaming source

Every accepted frame of a live session also goes to its streaming source. If that source fails
(the engine closed it, an error, a send that took over 3 s, or `[streaming] frame_backlog` undecided
frames), the session is **not** closed:
the source is dropped and the session keeps answering `realtime` and `chat` questions, while `past`
and `watch` questions get `stream_unavailable` and a running watch ends with an
`upstream_unavailable` row. Open a new session to get a streaming source back.

### Frame buffer

Each live session also keeps its most recent frames in memory, at most `[policy] buffer_frames`
(90) frames and `[policy] buffer_bytes` (12 MiB). Current-frame answers, the `past` fallback and the
offline subtask read from this buffer.

## Questions

`POST /v1/sessions/{sid}/text`

```json
{"text": "What is in front of the camera now?", "t": 12.3, "time_scope": "auto"}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `text` | string, not blank, at most 2000 characters | The question, in the user's words. Leading and trailing spaces are trimmed |
| `t` | number ≥ 0, at most 10^10 | When the question was asked, in session seconds (same clock as the frames). **Required for live sessions.** Optional for recordings (see [Offline recordings](#offline-recordings)) |
| `time_scope` | `"auto"` \| `"current"` \| `"past"` | Live only. `auto` lets the router decide. `current` sends the question to `realtime` and `past` to `past`, skipping the router. A stop phrase ("stop watching") still cancels the watch |

Reply `200`: `{"accepted": true, "t": 12.3}`. The answer arrives later as a result row.

A session answers **one question at a time**: a new question replaces one still being answered,
and the replaced question gets no answer row. A running watch is not affected by new questions,
except a new watch, which replaces it.

Each question has a time budget: `[policy] live_turn_s` (14 s) for live sessions,
`[policy] offline_turn_s` (145 s) for recordings. A question that runs out of time is answered with
status `request_timeout`.

A `realtime` question with no fresh frame is answered at once with `insufficient_evidence`; a
`past` question sent before any frame was accepted gets `stream_unavailable`.

### Current-frame freshness

A `realtime` question is answered from the newest buffered frame at or before the question's `t`,
and only if that frame is fresh: at most `[policy] current_frame_age_s` (2.5 s) older than `t` by
its own timestamp **and** received by the server at most 2.5 s before the question was. Otherwise
the answer has status `insufficient_evidence` and a message saying there is no frame yet or how old
the last one is. Every `realtime` answer carries `current_frame_check`:

| Field | Meaning |
| --- | --- |
| `status` | `locked` (fresh frame used), `stale_at_submission` (newest frame too old), `no_frame_at_or_before_question` |
| `pts` | `t` of the frame considered |
| `frame_index` | Its frame index |
| `source_age_s` | Question `t` minus frame `t` |
| `arrival_age_s` | Seconds between the frame's arrival and the question's |
| `submitted_monotonic` | Server clock when the question arrived (only useful for comparing rows) |

## Results

### `GET /v1/sessions/{sid}/results?after=<seq>&wait=<seconds>`

| Parameter | Default | Meaning |
| --- | --- | --- |
| `after` | `0` | Return rows with `seq` greater than this. Pass the largest `seq` you have processed |
| `wait` | `0` | Up to 60. When there is no row after `after`, wait this many seconds for one (long poll) |

```json
{"cursor": 9, "results": [{"seq": 8, "type": "answer", "...": "..."}, {"seq": 9, "...": "..."}],
 "controls_seen": 41}
```

The reply holds every row after `after`, including every row that arrived while waiting. `cursor` is
the newest `seq`. `controls_seen` counts the streaming model's decisions on the session's inputs so
far (a sign that the source is working). The session keeps its last 512 rows.

### `GET /v1/sessions/{sid}/events`

Server-sent events: first every row the session still holds, then each new row as it is published,
one `data: <json row>` per event. The stream ends when the session closes.

## Result rows

Every row has `seq` (increasing per session) and `type`.

| `type` | When | Fields |
| --- | --- | --- |
| `answer` | The answer to a question, a watch report, or a stop confirmation | See below |
| `caption` | Caption mode: the streaming model wrote a caption | `t` (time of the frame that prompted it), `text`, `response_id` |
| `warning` | Frame health got worse | `t` (last frame), `text`, `status`, `frames_10s`, `delay_s` |
| `closed` | The server closed the session | `t` (last frame), `reason` (`no_frames`), `text` |
| `error` | The session's event reader failed for good | `text` |
| `native_event` | Only with `native_events: true` | `event` (`input.done`, `response.created`, `response.done`, `context.compacted`), `source` (`observer`), `t`, and those of `input_id`, `response_id`, `status`, `control`, `reason`, `retained_turns`, `retained_visual_frames` the event has |

`answer` rows:

| Field | Always | Meaning |
| --- | --- | --- |
| `t` | yes | The end of `source_window_s` when there is one, else the question's `t` (`null` on the row that says a watch was interrupted) |
| `text` | yes | What to show or say to the user. For failures, a message in the user's language |
| `status` | yes | See [Statuses](#statuses) |
| `response_id` | yes | `harness-<hex>`, unique per row. The voice socket uses it to commit what was heard |
| `route` | yes | See [Routes, tasks and backends](#routes-tasks-and-backends). `null` on a `request_timeout` row and on the `upstream_unavailable` row of an internal error, which carry no route |
| `task` | yes | As above; `null` where `route` is |
| `backend` | no | `offline_model` or `streaming_session`; absent when no model answered |
| `vision_model` | offline-model answers | `[chat] model` |
| `route_decided_by` | live questions the harness answers itself | `rule` (a locale rule or `time_scope`), `model` (the router call), `fallback` (the router call failed, `realtime` was used). Absent on recording answers, on answers from the offline subtask, on watch reports and on timeout rows |
| `elapsed_s` | answers to questions | Seconds from the question to the answer. Absent on watch reports and timeout rows |
| `source_window_s` | when known | `[start, end]` in session seconds: the frames the answer was based on |
| `source_frame_indices` | offline-model answers with up to 8 frames | Frame indices of those frames |
| `video_frames` | offline-model answers with more than 8 frames | How many frames the answer was based on |
| `finish_reason` | offline-model answers | The model server's `finish_reason` (`stop`, `length`, ...). An answer cut at `length` ends at its last full sentence, or where a repeated list first comes round again |
| `current_frame_check` | `realtime` | See [Current-frame freshness](#current-frame-freshness) |
| `coverage` | `past` | `stream` (answered by the streaming source) or `buffer` (answered by the offline model from buffered frames) |
| `fallback_reason` | `past` with `coverage: buffer` | `stream_silent` or `stream_timeout` |
| `requested_window_s`, `native_receipt` | `past` from the stream | The window asked about, and the engine ids (`qa_session_id`, `qa_input_id`, `qa_response_id`) |
| subtask fields | subtask answers | Whatever the subtask adds, e.g. `rewrite_status`, `video_frames` |

### Routes, tasks and backends

| `route` | `task` | Who answers | `backend` |
| --- | --- | --- | --- |
| `realtime` | `realtime` | The offline model, with the current frame | `offline_model` |
| `chat` | `chat` | The offline model, no frame, with the persona and the recent conversation | `offline_model` |
| `past` | `past` | The session's streaming source; or the offline model with buffered frames when the stream stays silent | `streaming_session` or `offline_model` |
| `watch` | `watch` | Accepting the watch (no model), then the streaming model's reports | absent, then `streaming_session` |
| `cancel` | `cancel` | Nobody: the watch is stopped | absent |
| `offline` | the subtask's task name: `offline_direct`, `offline_video`, `offline_rewrite`, ...; `offline_subtask` when the subtask raised an error or returned no task name of its own | An offline recording, or a live `realtime`/`chat` question in a session opened with `offline_subtask: true` | usually `offline_model` |

How a route is chosen:

1. A stop phrase (e.g. "stop watching") → `cancel`.
2. `time_scope: "current"` → `realtime`; `time_scope: "past"` → `past`.
3. A watch phrase for one visible thing (e.g. "let me know when you see a dog") → `watch`.
4. A question that starts with a past marker (e.g. "Earlier, ...", "Just now ...") → `past`.
5. Otherwise one call to the offline model picks `watch`, `past`, `realtime` or `chat`. If that
   call fails or takes longer than `[policy] route_timeout_s` (2 s), `realtime` is used.

Rules of every active language apply to every question.

### `past`

The question goes into the session's streaming source as text, timestamped no earlier than the
newest frame, so the model has seen every frame up to the question. The streaming model answers
right away or chooses to stay silent.

- It answers: the row has `backend: "streaming_session"`, `coverage: "stream"`, and
  `source_window_s = [oldest frame the source held when it took the question, newest frame at the
  time of the question]`. Compaction drops old frames from the source, so the window start moves
  forward over a long session.
- It stays silent (in a caption session that includes taking the question as a caption step), or
  does not answer in time: the offline model answers from up to `[chat] past_images` (8) frames
  taken evenly from the session's buffer between the source window start and the question, with
  `backend: "offline_model"`, `coverage: "buffer"`, and `fallback_reason`. The stream gets the turn
  less 3 s, so the offline model has those 3 s. With no buffered frames the status is
  `insufficient_evidence`.
- It fails (the source is gone, the response did not complete, was cut at its length limit or was
  empty), or runs out of time with less than about 3 s of the turn left (when routing took most of
  the turn): `stream_unavailable`.

The streaming model may begin its answer with its own clock, e.g. `At 12 seconds: B`. The harness
removes such a prefix (English, or the Chinese form) before publishing the text, in `past` answers
and in watch reports.

### `watch`

The question is left in the streaming source; the streaming model decides when something worth
saying happens. The question gets an immediate row with status `watch_accepted`. Each later report
is a row with `route: "watch"`, status `model_report`, `backend: "streaming_session"` and
`source_window_s = [t, t]` at the moment it spoke.

- A watch phrase matched by a rule ("tell me when a dog appears") repeats: it reports every time.
- A watch chosen by the router call reports once and ends.
- One watch per session; a new watch replaces the old one. Pending responses triggered by the old
  watch's question or frames cannot become reports for the new watch.
- Stop it with a stop phrase (an answer row with route `cancel`) or
  `DELETE /v1/sessions/{sid}/watch`, which replies `{"stopped": true}` if a watch was running and
  `{"stopped": false}` otherwise.
- If the streaming source fails while a watch runs, a row with status `upstream_unavailable`
  says the watch was interrupted.

### Conversation memory

The offline model sees the last `[policy] dialogue_turns` (6) question and answer pairs, but only
for `chat` questions (with a picture it would repeat its earlier answers instead of looking). A pair
is remembered when the answer has status `ok` and is not a watch or cancel row. In a `voice` session
an answer is remembered only once the client reports that its audio finished playing; an answer the
user talks over is not remembered (see [voice-protocol.md](voice-protocol.md)).
An answer from the session's streaming source also goes back to that source when it is published
in a text session or confirmed played in a voice session.

## Statuses

| `status` | Meaning |
| --- | --- |
| `ok` | An answer |
| `insufficient_evidence` | No usable frame: none received yet, the newest is too old for a "now" question, or a recording or buffer had no frames to look at |
| `upstream_unavailable` | The offline model or the offline subtask failed (error or bad reply), the harness hit an internal error, or a running watch was interrupted. Asking again may work |
| `stream_unavailable` | The session's streaming source is gone, has not been sent a frame yet, or gave no usable answer to a `past` question or a watch. If the source is gone, open a new session |
| `request_timeout` | The question ran out of its turn time |
| `invalid_visual_format` | The offline model replied with empty text |
| `watch_accepted` | A watch was started |
| `model_report` | A watch report from the streaming model |
| `cancelled` | Reserved. This version does not publish it: a question replaced by a newer one gets no row |

A stop phrase is answered with status `ok` and text saying the watch was stopped, or that there was
no watch to stop. A custom offline subtask may return other statuses of its own.

## Offline recordings

1. `POST /v1/sessions` with `{"input_mode": "offline"}`.
2. `POST /v1/sessions/{sid}/video` with the raw file as the body and `Content-Type: video/mp4`,
   `video/quicktime` or `application/octet-stream`. Reply `201`:

   ```json
   {"input_mode": "offline", "bytes": 1048576, "source_fps": 25.0, "source_frames": 150,
    "last_frame_pts_s": 5.96}
   ```

3. `POST /v1/sessions/{sid}/text` with `text` and optionally `t`. Without `t` the question is about
   the whole recording. With `t`, it is about the recording up to that second; a `t` beyond the
   last frame (by more than one frame) is `422 playback timestamp exceeds the recording`.
   `time_scope` must be `auto`.

Recordings must be constant-frame-rate H.264 or HEVC in an MP4/MOV container, at most
`[media] max_upload_bytes` (128 MiB) and `[media] max_recording_s` (1800 s) long, at most
4096 × 2160 pixels in area, at most 120 fps and at most 108,000 frames. One recording per session;
the server takes one upload at a time. The server needs `ffmpeg` and `ffprobe`
([configuration.md](configuration.md)).

Questions about recordings are not routed: they all go to the offline subtask, so their rows have
`route: "offline"`. Clients send no sampling settings. The default subtask decodes the recording once,
right after the upload, at a rate and size chosen from its length and resolution (one frame a second
at about 480 × 270 up to 256 s; at least 256 frames, at most one every two seconds, at about
200,000 pixels for longer recordings; see [design.md](design.md#reading-a-recording)). A question
gets the frames from the start of the recording to `t`, each behind its time (`<12.0 seconds>`),
with the question, so an answer can say when something happens. The first question after an upload
may wait for the decode (about 11 s for five minutes of 720p).

## Session logs

Live `qa` sessions keep a log of what the server saw: frames received, questions, answers, polls and
rejected requests. A client can read it while it uses the session, to debug "the answer seems to be
about an old frame".

```
GET /v1/sessions/{sid}/logs?after=0&limit=100
X-Session-Log-Token: <logs_token from the open reply>
```

| Parameter | Default | Meaning |
| --- | --- | --- |
| `after` | `0` | Return log entries with `log_seq` greater than this; pass the previous page's `next_after` |
| `limit` | `100` | 1 to 250 |

```json
{"session_id": "3f0c...", "events": [...], "next_after": 37, "first_available": 1,
 "truncated": false, "has_more": false}
```

Each entry has `log_seq`, `time` (UTC, ISO 8601) and `event`:

| `event` | Fields |
| --- | --- |
| `opened` | `mode`, `voice`, `fps` |
| `frame` | `t`, `bytes`, `accepted`, `reason` |
| `question` | `question_t`, `text`, `input` (`text` or `voice`) |
| `answer`, `caption`, `error` | `seq`, `t`, `response_id`, `status`, `source_window_s`, `route`, `task`, `text`; and `question_log_seq`, `question_t` linking it to its question |
| `poll` | `after`, `returned_seq` (the `seq` of each row the poll returned) |
| `request_error` | `operation` (`frame`, `text`, `results`), `http_status` |

The log keeps the last 1024 entries; `truncated: true` means older entries were overwritten and
`first_available` is the oldest one left. The log never contains image or audio data. A missing or
wrong token, a closed or closing session, or a caption or offline session all give
`404 session log unavailable`. Keep the token out of URLs and ordinary application logs.

## Errors

| Status | `detail` | Route | Cause |
| --- | --- | --- | --- |
| 404 | `unknown session` | any session route | No such session, or it was closed |
| 404 | `session closed: no frame for 10 s` | any session route but `/logs` | The server closed the live session for lack of frames (`409` with the same `detail` while the close is still running, except on `DELETE`, which waits for it) |
| 409 | `session is closing` | any session route | `DELETE` is in progress |
| 409 | the failure, e.g. `RuntimeError(...)` | any session route | The session failed and must be closed |
| 503 | `no streaming capacity for a live session` | open | `user_streams` live sessions are open, or the streaming engine refused or did not answer within 5 s |
| 503 | `offline session limit reached` | open | `max_offline_sessions` offline sessions are open |
| 422 | `need max_visual_frames <= 700 and 1 <= compact_to_visual_frames <= min(max_visual_frames, 100)` | open | Frame limits out of range (the first number is `[streaming] max_visual_frames_limit`) |
| 422 | field errors | open | An unknown field, e.g. one of the removed options |
| 409 | `upload offline recordings to /video` | frame | Frames sent to an offline session |
| 413 | `frame exceeds the size limit` | frame | Body larger than `max_frame_bytes` |
| 408 | `frame upload timed out` | frame | The body took longer than 3 s to arrive |
| 415 | `body must be a JPEG or PNG image` | frame | The body is not JPEG or PNG |
| 422 | `frame timestamp out of range for the session's fps` | frame | `t` is not a finite number, or `t * fps` is not below 2^63 (a missing or negative `t`, or one above 10^10, is a validation error) |
| 409 | the streaming error | frame | Sending to the streaming source failed and the source could not be dropped |
| 404 | `unknown session` | frame, video | The session was closed while the body was arriving (or the idle reason above) |
| 409 | `open an offline session to upload a recording` | video | Upload to a live session |
| 415 | `send a raw MP4/MOV body` | video | Wrong `Content-Type` |
| 409 | `this session already has a recording or upload` | video | Second upload |
| 503 | `another recording is being uploaded` | video | The server takes one upload at a time; retry later |
| 413 | `recording exceeds the upload size limit` | video | Larger than `max_upload_bytes` |
| 422 | `recording is empty` | video | Empty body |
| 422 | `expected a constant-frame-rate H.264 or HEVC MP4/MOV recording` | video | Not a supported recording, too long, too large, or variable frame rate |
| 422 | `video preprocessing failed` | video | `ffprobe` could not read the file |
| 503 | `video decoder is unavailable` | video | `ffprobe` is not installed or not found |
| 504 | `recording inspection timed out` | video | Upload and inspection took more than 200 s |
| 409 | `upload the recording first` | text, audio | Question before the recording |
| 422 | `live questions require t` | text | Live question without `t` |
| 422 | `time_scope applies to live sessions` | text | `time_scope` other than `auto` on a recording |
| 422 | `invalid question` | text | Blank, longer than 2000 characters, or a `t` that is not a finite number |
| 422 | `playback timestamp exceeds the recording` | text | `t` past the end of the recording |
| 409 | `session cannot run a watch` | watch | `DELETE /watch` on an offline session |
| 404 | `session log unavailable` | logs | Wrong or missing token, closed session, or no log for this session (caption and offline sessions have none) |
| 503 | `session cleanup unconfirmed` | close | Part of the session did not shut down cleanly; it is removed anyway |
| 422 | field errors | any | Request body or query failed validation (e.g. `wait` > 60, unknown `time_scope`) |
| 500 | `Internal Server Error` (plain text) | any | A bug in the server; the server logs it |

## Worked example with curl

Needs `curl` and `jq`, and a running server (`ov25-harness serve --config configs/local-fake.toml`).

```sh
BASE=http://127.0.0.1:8100

# Open a live QA session.
OPEN=$(curl -fsS -X POST "$BASE/v1/sessions" -H 'Content-Type: application/json' \
  -d '{"input_mode": "live", "mode": "qa", "fps": 30}')
SID=$(jq -er .session_id <<< "$OPEN")

# Send the same picture as frames at t = 0, 1, 2, 3, one a second.
for T in 0 1 2 3; do
  curl -fsS -X POST "$BASE/v1/sessions/$SID/frame?t=$T" \
    -H 'Content-Type: image/jpeg' --data-binary @tests/data/frame.jpg | jq -c '{accepted, health}'
  sleep 1
done

# Ask about the current frame, at t = 3.2.
curl -fsS -X POST "$BASE/v1/sessions/$SID/text" -H 'Content-Type: application/json' \
  -d '{"text": "What is in front of the camera now?", "t": 3.2, "time_scope": "current"}'

# Wait up to 10 s for the answer.
curl -fsS "$BASE/v1/sessions/$SID/results?after=0&wait=10" \
  | jq '.results[] | select(.type == "answer") | {route, status, text, source_window_s}'

# Close.
curl -fsS -X DELETE "$BASE/v1/sessions/$SID"
```

A real client keeps sending one frame a second for as long as the session is open; this example
asks soon enough after its last frame that the session is still alive and the frame is still fresh.
