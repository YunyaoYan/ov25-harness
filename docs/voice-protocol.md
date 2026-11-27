# Voice protocol

A session opened with `"voice": true` can carry speech over one WebSocket:

```
WS /v1/sessions/{sid}/audio
```

The microphone goes up as raw PCM, the user's questions are transcribed by an ASR server and asked
like text questions, and every answer comes back down as synthesized speech. Frames still go
through `POST /frame`; the socket carries only audio and control messages. Answers also keep
appearing in `/results` and `/events` as usual.

The server needs `[voice] enabled = true` and reachable ASR and TTS servers
([configuration.md](configuration.md)).

## Formats

- **Binary WebSocket messages** are audio: 16-bit signed little-endian mono PCM (`s16le`), with no
  header. Up: at the input rate from `hello`. Down: at the output rate from `hello`.
- **Text WebSocket messages** are JSON objects with a `type`.
- Times `t` are session seconds, the same clock as frames and text questions.

## Connecting

The server accepts the socket, then closes it right away with one of these codes if it cannot be
used:

| Close code | Reason |
| --- | --- |
| 4404 | `unknown session`: no such session, or it is closing (when the server closed it for lack of frames, the reason is that, e.g. `session closed: no frame for 10 s`) |
| 4400 | `open the session with voice: true` |
| 4409 | `upload the recording first`: an offline session without its recording |
| 4500 | `this service has no voice backends`: voice is not enabled on the server |
| 4409 | `this session already has audio`: one audio socket per session |
| 4422 | `first message must be hello`, or `rate out of range` (see below); sent after `hello` is read |

### `hello` (client → server, first message)

```json
{"type": "hello", "rate": 16000, "out_rate": 24000, "vad": "client", "t": 3.0}
```

| Field | Default | Meaning |
| --- | --- | --- |
| `rate` | `16000` | Sample rate of the microphone PCM you send, 8000 to 48000 |
| `out_rate` | same as `rate` | Sample rate you want speech in, 8000 to 48000. Speech is resampled from the TTS rate when they differ |
| `vad` | `"client"` | `"client"`: you mark each utterance with `speech.begin` / `speech.end`. `"server"`: you stream the microphone continuously and the server finds the speech |
| `t` | `0` | Session time of the first audio sample you will send |

Anything else as the first message (a binary message, invalid JSON, another `type`) closes the
socket with 4422 `first message must be hello`. A `rate`, `out_rate` or `t` that is not a finite
number, a rate outside 8000 to 48000, or a `t` outside 0 to 10^10 (the bound HTTP times have),
closes it with 4422 `rate out of range`.

### `ready` (server → client)

```json
{"type": "ready", "session_id": "3f0c...", "in_rate": 16000, "out_rate": 24000,
 "format": "s16le", "vad": "client", "tts_rate": 24000}
```

`tts_rate` is the TTS server's native rate (`[voice] tts_rate`).

## Speaking to the server

The server keeps an audio clock: it starts at `hello.t` and advances by the duration of every
binary message received. A question's time is the `t` of its `speech.end` (client VAD) when given,
otherwise this clock.

### Client VAD (`"vad": "client"`)

```
speech.begin {"type": "speech.begin", "t": 12.0}
<binary PCM> <binary PCM> ...
speech.end   {"type": "speech.end",   "t": 14.1}
```

PCM sent outside a `speech.begin` / `speech.end` pair is ignored (it still advances the clock).
An utterance longer than 30 s is dropped with a non-fatal `error` whose `detail` is the locale's
message (in English, `A spoken turn can be at most 30 seconds.`). The `t` of `speech.end` is
optional; one that is not a number from 0 to 10^10 (the bound HTTP times have) drops the
utterance with a non-fatal `error` `t must be a number from 0 to 10^10`.

### Server VAD (`"vad": "server"`)

Send microphone PCM continuously. The server runs a simple energy detector tuned for a microphone
close to the mouth; it is less robust than a client-side detector in a noisy room. When it hears
speech start it sends:

```json
{"type": "speech.detected", "t": 12.0}
```

and keeps about one second of audio from before the detection; between utterances it keeps nothing
older than that. About 0.8 s of silence ends the utterance. An utterance of more than 30 s from the
detection on, not counting the second kept from before it or the 0.8 s of silence that ends it, is
dropped with the same non-fatal `error` as with client VAD, and its end is not a question.
`speech.begin` and `speech.end` from the client are ignored in this mode.

### What happens to an utterance

1. **Speech starts** (`speech.begin` or detection): the server stops speaking at once (see
   [Interruptions](#interruptions)). Answers waiting to be spoken are kept for now: the sound may
   not be the user.
2. **Speech ends**: an utterance shorter than 350 ms is dropped as noise. Otherwise the frames the
   question will be answered from are fixed now, at the end of speech, and the audio goes to ASR.
3. The server sends the transcript:

   ```json
   {"type": "transcript", "t": 14.1, "text": "What is on the table?"}
   ```

4. A transcript with no words (empty, punctuation only, or one bracketed tag such as `<BGM>`) is
   dropped, and the waiting answers are spoken as if nothing happened.
5. Otherwise it is a question: answers still waiting to be spoken are dropped (they answer an older
   question), and the question is submitted exactly like `POST /text` with `t` = the utterance time
   (for an offline session, a question about the whole recording). A question that is rejected
   (e.g. too long) gives a non-fatal `error` whose `detail` is the locale's message (in English,
   `Invalid question.`).

ASR failures give a non-fatal `error` with `detail` starting `asr:`.

## Answers and speech

Every `answer` row of the session is sent on the socket and then spoken; `caption` rows are sent but
not spoken:

```json
{"type": "answer", "response_id": "harness-7c1e...", "t": 14.0, "text": "A red cup.",
 "status": "ok", "route": "realtime", "task": "realtime"}
{"type": "caption", "response_id": "...", "t": 20.0, "text": "..."}
```

Answers are spoken one at a time, never while the user is speaking. Each answer is split into
sentences and each sentence is one speech segment:

```
{"type": "audio.begin", "speech_id": "sp_4", "rate": 24000, "format": "s16le", "text": "A red cup."}
<binary PCM> <binary PCM> ...
{"type": "audio.end", "speech_id": "sp_4", "samples": 30720, "ms": 1280}
```

- `speech_id` is unique per sentence on this socket (`sp_1`, `sp_2`, ...).
- `rate` and `format` repeat the output format.
- `samples` is the number of samples in this sentence's audio.
- `ms` is the total audio sent for the answer so far, up to the end of this sentence.

If the TTS server fails, the server sends `{"type": "error", "detail": "tts: ...", "fatal": false}`
and skips the rest of that answer. In this version the answer then still counts as being spoken:
later answers wait until the user next speaks (a known issue).

### Playback reports (client → server)

| Message | When to send it |
| --- | --- |
| `{"type": "play.done", "speech_id": "sp_4"}` | You finished playing a segment. **Send it for every segment.** The next answer is spoken only after `play.done` for the last segment of the current one |
| `{"type": "play.progress", ...}` | Optional; accepted and ignored |
| `{"type": "cancel", "played_ms": 640}` | You stopped playback early; `played_ms` is how much of the current answer's audio was played. One that is not a finite number gets a non-fatal `error` `played_ms must be a finite number` |

### Interruptions

When speech starts while an answer is being spoken, the server stops sending its audio and, if a
segment was open, sends:

```json
{"type": "audio.cancel", "speech_id": "sp_5"}
```

Stop playing that segment and discard what you have buffered of it.

### What is remembered as heard

The offline model's conversation memory (used for `chat` questions) should contain what the user
actually heard, not what the model wrote. In a voice session:

- An answer is remembered **in full** when `play.done` arrives for its last segment.
- An answer **interrupted** by the user is not remembered: the interruption is settled at once,
  before any playback report could arrive, so nothing of it is kept. A `cancel` message is accepted,
  but in this version it does not add anything to memory.
- Answers dropped because a new question arrived before they were spoken are not remembered.
- Watch and cancel rows, and rows whose status is not `ok`, are never remembered (as in text
  sessions).

## Other messages

| Direction | Message | Meaning |
| --- | --- | --- |
| client → server | `{"type": "bye"}` | Close the socket. The server replies `{"type": "closed"}` and closes it |
| server → client | `{"type": "error", "detail": "...", "fatal": false}` | Something failed; the socket stays usable. Also sent for a second `hello` (`already said hello`) and for text messages that are not JSON (`not JSON`) |
| server → client | `{"type": "error", "detail": "...", "fatal": true}` | The session itself failed (its `error` row). Close the session |

Unknown message types are ignored. When the session closes, the socket closes.

## A minimal client exchange

```
→ {"type":"hello","rate":16000,"out_rate":16000,"vad":"client","t":5.0}
← {"type":"ready",...}
→ {"type":"speech.begin","t":6.0}
→ <PCM 1.5 s>
→ {"type":"speech.end","t":7.5}
← {"type":"transcript","t":7.5,"text":"What color is the cup?"}
← {"type":"answer","text":"The cup is red.","status":"ok",...}
← {"type":"audio.begin","speech_id":"sp_1",...}
← <PCM> ...
← {"type":"audio.end","speech_id":"sp_1","samples":17600,"ms":1100}
→ {"type":"play.done","speech_id":"sp_1"}
→ {"type":"bye"}
← {"type":"closed"}
```

`ov25-harness check URL IMAGE --voice` runs a similar exchange at 16 kHz and 24 kHz.
