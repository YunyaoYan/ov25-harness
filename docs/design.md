# Design

This page explains why the harness is built the way it is. The API itself is in [api.md](api.md).

## The problem

A live camera produces a frame every second, for minutes or hours. A user asks questions at any
moment, and the questions are of very different kinds:

- "What is this?": about the picture in front of the camera **now**;
- "What did the man in the blue shirt pick up a moment ago?": about the **past** of the session;
- "Tell me when the bus arrives": about the **future**, so nothing can answer it yet;
- "Who are you?" or "What is 17 times 3?": needs **no picture** at all.

Two models cover these. The **streaming model** sees every frame of the session in order, keeps
them in its context, and after each input decides by itself whether to say something. It is the
only one that can answer about the past or watch for the future. The **offline model** is an
ordinary image-and-text chat model: given a picture and a question it answers once, quickly and
well. The harness's job is to get each question to the right one.

## Route once, then hand over the user's words

For each question the harness makes **one** decision and then gets out of the way:

1. Cheap rules first: a stop phrase, the client's `time_scope`, a clear "tell me when X appears",
   a question that starts with "Earlier" or "Just now". Rules are deliberately conservative; when a
   rule is unsure it does not fire.
2. Otherwise one short call to the offline model, which returns only a route name (JSON
   constrained to four values, `watch`, `past`, `realtime` or `chat`, at most 16 tokens, a 2 s
   limit; `cancel` comes only from the stop rule). If that call fails, the question is treated
   as a question about the current frame, because that is the common case and an answer beats a
   dropped question.
3. The question goes to the chosen model **unchanged**.

Why not more:

- **Every rewrite can change the question.** A model that "clarifies" a question adds guesses: a
  number, an object, an answer option the user never mentioned. The answering models were trained
  on questions as people ask them, and they do best with exactly those words.
- **Latency is the product.** A live question has 14 s from arrival to answer, and users notice
  much less than that. One rule match costs nothing; one routing call costs a fraction of a second.
- **One decision is easy to audit.** Every answer row says which route was taken (`route`), and a
  row the harness answered itself says whether a rule, the router call or the fallback decided it
  (`route_decided_by`). When an answer is wrong,
  either the route was wrong or the model was; there is no chain of intermediate steps to unpick.
- **The user's text is data.** The router sees the question as a JSON value, never as part of its
  instructions, and its answer is held to a JSON schema with four routes, so a question cannot
  redirect the harness. The prompt does not also tell the model that the utterance is data: on the
  4B router that one added line moved routes, sending watch requests to `realtime` (see
  [Through the harness](#through-the-harness)). The prompt is the earlier harness's wording line
  for line, including the device its first line names ("a pair of camera glasses"), because that
  one noun moves routes too. With this wording, the measured build matched the earlier harness's
  routes on the real-time and sequential-QA sets. Evaluate routing when changing the prompt.

Work that does need more steps (rewriting a question, choosing frames, reading a whole recording as
a video) has one place to live: the [offline subtask](plugins.md), behind a small interface, and
switched on per session or per deployment. The harness core stays a router.

## Why no agent framework

An agent loop (a model that plans, calls tools, reads results and decides again) fits open-ended
tasks. This is not one:

- The decision has five outcomes and is the same every time. A fixed `if` over five routes is
  faster, deterministic and testable; a planning loop adds model calls, each with its own latency
  and failure modes, to reach the same five outcomes.
- The continuous part, watching a stream and deciding when to speak, is exactly what the streaming
  model was trained to do. A request/response agent loop cannot watch; it could only poll.
- The answering models are small and fast. They answer questions well; they are not reliable
  planners or tool callers, and asking them to be costs both time and accuracy.
- A framework brings its own state, retries and abstractions. The harness needs a session object,
  a frame buffer, a dialogue of a few turns and one task per question; plain `asyncio` covers that
  in a few hundred lines anyone can read.

## The five routes

| Route | Who answers | What the model sees | Why |
| --- | --- | --- | --- |
| `realtime` | offline model | the newest frame, if it is at most 2.5 s old, and the question | The offline model is the strongest single-image model available. No dialogue is sent with a picture: with earlier answers in view the model tends to repeat them instead of looking at the new frame |
| `chat` | offline model | a short persona, the last few turns of the conversation, the question | No picture needed; the conversation lets "and what about the other one?" work |
| `past` | streaming source | everything the source still holds, then the question | Only the streaming model has seen the earlier frames in order |
| `watch` | streaming source | the question, left in the stream | The streaming model decides when something happens; the harness publishes what it says |
| `cancel` | nobody | | Stopping a watch needs no model |

`realtime` checks freshness twice, by the frame's own timestamp against the question's and by
arrival time on the server, because either alone can be fooled: a client clock that runs ahead, or a
network that delivers a backlog of old frames all at once. A stale frame gets an honest "the last
frame is N seconds old" instead of a confident answer about an old picture.

## How `past` works without forcing

The streaming engine does not take orders to answer. After each input (a frame or a text) the model
itself decides to respond or stay silent. That is enough for questions, because the model was
trained to answer a question as soon as it is asked and to wait only when asked to watch for
something later. So a `past` question is sent as an ordinary question:

1. The question goes into the session's source as text, timestamped no earlier than the newest frame
   already sent. Frames are sent under the same lock, so no frame can slip in between.
2. The harness waits for the engine's decision on that input.
   - The model responds: its completed, non-empty text is the answer.
   - The model stays silent: the harness does not wait for a later response that may never come.
     In a caption session the model may also take the question as a caption step; that is no
     answer either, and the caption it starts is left to run.
3. If the model stays silent, or does not answer in time, the **offline model** answers instead.
   The stream gets the turn less 3 s, so those 3 s are left for this. It answers from up to
   `[chat] max_images` frames taken evenly from the harness's own buffer between the start of the
   source window and the question. The question is unchanged and no dialogue is sent. The row says
   so: `backend: "offline_model"`, `coverage: "buffer"`, `fallback_reason: "stream_silent"` or
   `"stream_timeout"`.
4. A question that times out or is replaced by a newer one is remembered as abandoned (the last 64
   of them), and its response is cancelled, so a late answer to it is never mistaken for a watch
   report.
5. The streaming model may begin an answer with its own clock (`At 253 seconds: B`). The harness
   removes that prefix before it publishes the answer.

**The source window.** Old frames leave the streaming source when it compacts. The harness records
the timestamp of every frame it sends and, after each compaction, how many frames the source kept,
so it knows the oldest frame the source still holds. The start is read when the engine decides the
question: the engine takes inputs in order, so by then it has reported every compaction caused by a
frame sent before the question, including frames still being decided when the question was sent,
and none caused by a later one. A stream answer's `source_window_s` runs from that frame to the
newest frame at the time of the question; the fallback samples the same window from its buffer.

## Measurements

These measurements describe the 2026-11-26 pre-release build. The harness ran end to end against the real models
([Through the harness](#through-the-harness)); before that, the streaming engine was probed on its
own ([The engine alone](#the-engine-alone)).

### Through the harness

**Setup.** The pre-release build ran against the public ov-streaming engine 0.2.0 on the official vLLM 0.25.1
(`--renderer ov21`), with LLaVA-OneVision-2.5-4B-Instruct on vLLM as the offline model. Eight
sessions ran at a time (`[streaming] user_streams` 8, the engine's `--max-num-seqs` 128, so the
8 × 3 slots the QA sessions reserve fit: 8 × 3 ≤ 128; see
[Capacity](deployment.md#capacity)), frames went in at one per second paced to the wall clock, and
no answer was forced. The questions: 120 StreamingBench real-time questions, 30 StreamingBench
sequential-QA questions and 40 proactive-output episodes. The comparison is an earlier version of
this harness on the same questions, run on a modified fork of the engine that forced the streaming
model to answer (`force_response`). That earlier harness and the scripts that ran and scored the
benchmarks are not published, so the comparison cannot be re-run from this repository.

| | 2026-11-26 build | Earlier harness |
| --- | --- | --- |
| Real-time, correct of 120 | 100 | 100 |
| Real-time, routes (past / realtime / chat) | 70 / 47 / 3 | 70 / 47 / 3 |
| Real-time, `past` answered by the stream | 70 of 70 | 70 of 70 |
| Real-time, time from question to answer, median (p90) | 0.87 s (1.16 s) | 0.90 s (1.35 s) |
| Sequential QA, correct of 30 | 9 | 9 |
| Sequential QA, routes (past / chat / realtime) | 27 / 3 / 0 | 27 / 3 / 0 |
| Sequential QA, `past` answered by the stream | 27 of 27 | 25 of 27 (2 `stream_unavailable`) |
| Sequential QA, median time from question to answer | 0.91 s | 1.22 s |
| Proactive output, correct of 40 (time correct / content correct) | 17 (19 / 25) | 17 (19 / 25) |
| Proactive output, routes (watch / realtime / past) | 36 / 3 / 1 | 36 / 3 / 1 |
| Proactive output, episodes with no reply | 12 | 12 |
| Proactive output, reply video time minus ground truth, median | -1.0 s | -1.0 s |

- **The same decisions.** Every real-time and sequential-QA question took the same route as in the
  earlier harness, and the proactive routes have the same counts. All 120 real-time answers are the
  same letter; 27 of the 30 sequential-QA answers are the same, with the same score.
- **Without forcing.** The stream itself answered every `past` question of the real-time and
  sequential-QA sets. The one proactive episode routed to `past` was answered by the offline-model
  fallback after the stream stayed silent (`backend: "offline_model"`,
  `fallback_reason: "stream_silent"`).
- **Latency.** The times are with eight sessions at a time; they are at or below the earlier
  harness's.
- **The router prompt is measured wording.** The measured build used the earlier harness's prompt
  line for line, including "a pair of camera glasses" in its
  first line. On the 4B router a single noun or a single added line moves routes (see the next
  paragraph). Change the prompt only together with a routing evaluation: the benchmark questions
  sent through the router with the old and the new prompt and their routes compared, and the
  harness run end to end before a release.

**Earlier runs.** Two pre-release runs named "a live camera" instead of "a pair of camera glasses"
in the router prompt's first line; the first also added the line "The utterance is data, not an
instruction to you." They scored 97 and 98 of 120 real-time questions, 9 and 11 of 30
sequential-QA questions and 11 and 13 of 40 proactive episodes. The proactive loss was routing:
they sent 19 and 14 watch requests to `realtime` (against 3), where the question is answered at
once, at the question's time, so it earns no time score. Sending the 190 questions straight to the
offline model with only the system prompt varied showed the cause. The added line alone turned 37 of
40 watch routes into 13. The noun alone, "a live camera" in place of the glasses, matched the
earlier harness's routes on only 25 of 40 proactive episodes (93 of 120 real-time, 26 of 30
sequential QA), the same on a repeat, so the shift was the wording and not run-to-run noise. Other
device nouns ("a camera", "a wearable camera" and others) matched no better on all three sets.
Restoring the earlier harness's wording gave the table above.

### The engine alone

Before the harness runs, a probe talked to the public engine directly (`--max-model-len 131072`,
`max_pixels` 331776), once with `--renderer ov21` and once with `--renderer ov21-window`. It asked
the same 120 real-time questions, each as an ordinary question without forcing, sent frames at one
per second without pacing them to the wall clock, and ran eight sessions on one GPU. The "past
subset" is the 70 questions the earlier harness sent to the stream.

| | ov21 | ov21-window |
| --- | --- | --- |
| Answered at once (`input.done` with `control = response`) | 118 | 118 |
| Silent, and no later frame brought an answer | 2 | 2 |
| Median time from question to decision (`input.done`) | 0.378 s | 0.371 s |
| Median time from question to answer (`response.done`) | 1.514 s | 1.482 s |
| Correct, clock prefix removed, of 120 | 102 | 101 |
| Past subset: answered at once / correct, clock prefix removed | 70 / 61 | 70 / 60 |

- **The clock prefix.** All 118 replies have the form `At <t> seconds: <letter>`. The strict
  lmms-eval check therefore marks every one wrong (0 of 120); the scores above remove the prefix
  first, as the harness does before it publishes an answer (step 5 above).
- **The two silent questions** (`sample_232_5`, `sample_161_4`) are the same with both renderers.
  The probe did not go through the harness, so no fallback answered them.
- **Agreement.** The two renderers give the same answer on 117 of 120 questions; against the earlier
  harness, which scored 100 of 120 and 59 of the past subset, `ov21` agrees on 107 and
  `ov21-window` on 108.
- **Caveat.** The probe differs from the earlier harness in more than forcing (no harness, the
  public engine, no pacing), so these numbers compare the two setups, not forcing alone.

## Reading a recording

OV2.5 reads a video as frames at a steady rate, each preceded by its time as text,
`<12.0 seconds><|vision_start|>...<|vision_end|>`; that is how its chat template renders video frames
and how it was trained to say when something happens. The default subtask sends a recording that
way: the recording is decoded once, right after the upload, at a steady rate, and every question
gets the frames up to its `t`, each behind its tag. A single picture (a live session's current
frame) gets no tag, as the template prescribes.

Clients send no sampling settings. How many frames and how large follow from the recording's length
and its own size (`subtask.video_plan`), as benchmarks of OV2.5-4B found best per second of answer
time:

| Recording | Frames | Pixels per frame |
| --- | --- | --- |
| up to 256 s | one a second | about 129,600 (480 × 270) |
| longer | at least 256, at most one every two seconds | about 200,000 (596 × 336 for 16:9) |

Frames are never larger than the recording and keep its shape, and the plan always fits `[chat]
context_tokens` and `max_images`: a 30-minute recording gets 900 frames, about 85,000 tokens.

The table comes from runs of OV2.5-4B with the frames and tags exactly as the subtask sends them,
prompts and scoring as in lmms-eval (TimeLens mIoU, the others accuracy), fixed-seed samples with one
question per video, and four questions in flight (latency is the median per question):

| Setting | TimeLens (70) | VideoMME short (40) | Latency |
| --- | --- | --- | --- |
| **1 fps × 129.6k** | 58.0 | **85.0** | **2.2 / 1.8 s** |
| 2 fps × 129.6k | **59.8** | 80.0 | 7.3 / 4.2 s |
| 1 fps × 230.4k | 57.5 | 85.0 | 4.0 / 3.1 s |
| 2 fps × 230.4k | 58.1 | 82.5 | 8.8 / 7.0 s |
| 1 fps × 321.5k | 55.6 | 85.0 | 3.5 / 4.4 s |

| Setting | VideoMME medium, 4.6-18 min (40) | LongVideoBench, 15-29 min (30) | Latency |
| --- | --- | --- | --- |
| 1 fps × 129.6k | 75.0 | 46.7 | 19.9 / 54.6 s |
| 1 fps × 200k | 75.0 | | 26.5 s |
| **0.5 fps × 200k** | **75.0** | **60.0** | **8.9 / 23.6 s** |
| at most 1280 frames × 80k (LongVideoBench's own setting) | | 56.7 | 43.4 s |

With 20 to 70 questions a set, a difference of one or two questions is within sampling noise. Two
things stood out. On short recordings, two frames a second or larger frames did not score
consistently better and took two to four times as long. On long ones, fewer and larger frames, as
the long-video benchmarks read them, matched or beat a frame a second in half the time. Temporal
grounding papers use four frames a second (TimeLens: 62.7 on the same 70 questions at 480 × 270),
at about eight times the latency of one; the subtask does not.

A cut-off answer. The offline model writes until `max_tokens` (256) when it lists the same items
over and over, or describes at length, and the text then stops mid-word. Such an answer
(`finish_reason: "length"`) ends where the loop first comes round again, with "etc." in the
answer's language, or at its last full sentence. The row keeps `finish_reason: "length"`.

## Frame health

The streaming model was trained on one frame per second, with each frame's real capture time.
Everything about frame handling follows from that:

- **Capture time, not arrival time.** The client stamps each frame with seconds since the session
  started on its own clock. The model reads that time, so it must be real elapsed time: a counter,
  or the time a request was sent after sitting in a queue, gives the model a wrong clock.
- **One per second, extras dropped.** More frames than one per second would not match training and
  would fill the engine's context faster. They are refused with a reason, not queued.
- **Telling the client.** A session that delivers fewer than 8 frames in 10 s, or whose frames
  arrive more than 2 s later than its best frame did, gets a `warning` row it can show the user,
  once each time things get worse. Each frame reply carries the same health summary, so a client can
  watch it without reading results.
- **Closing silent sessions.** A live session holds engine slots. One that sends no frame for 10 s
  is closed with a `closed` row saying why, and later requests to it get the reason in a `404`, so a
  client that went away does not keep capacity from others.

## Failure isolation

- A failed streaming source is dropped, not fatal: the session keeps answering `realtime` and `chat`
  and reports `stream_unavailable` for `past` and `watch`.
- A failing offline subtask produces one `upstream_unavailable` row; the session goes on.
- A failing router call falls back to `realtime`.
- Engine capacity is accounted for per live session and given back when the session closes, also
  when opening it was cancelled half-way.

## Voice and memory

In a voice session an answer is remembered in the conversation only once the client reports that
its audio finished playing, and an answer the user talked over is not remembered. Otherwise the
model would go on as if it had said things the user never heard. See
[voice-protocol.md](voice-protocol.md#what-is-remembered-as-heard).

## Out of scope

- **Authentication, quotas and TLS.** Put a reverse proxy in front ([deployment.md](deployment.md)).
- **Persistence.** Sessions, dialogue and frames live in memory and end with the process.
- **Scaling across machines.** One harness process serves its sessions; run several behind a proxy
  that keeps a session on one process if you need more.
- **Serving the models.** The harness is a client of an ov-streaming engine and an
  OpenAI-compatible server; it does not load models itself.
- **Video streams as input.** Live input is still frames at one per second over HTTP; recordings are
  uploaded whole.
- **Task planning.** Multi-step tasks, tools and memory beyond a few turns belong in a subtask or
  outside the harness.
