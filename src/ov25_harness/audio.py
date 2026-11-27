"""The voice WebSocket: microphone PCM in, speech out, as docs/voice-protocol.md describes it.

The socket carries 16-bit mono PCM both ways, with JSON text frames for control. Video frames do
not go through it; they keep using POST /frame.
"""
import asyncio
import contextlib
import json
import logging
import math
import re
import time

from starlette.websockets import WebSocketState

from .harness import MAX_T_S
from .resample import Resampler
from .voice import pcm as pcmlib
from .voice.answers import Commit, Interrupt, Speak, SpokenAnswers

_LOG = logging.getLogger(__name__)
# Shorter than this and a segment is room noise, not a question. Qwen3-ASR
# answers a fragment with a plausible line rather than an empty string.
MIN_UTTERANCE_MS = 350
# Longest spoken turn, with either VAD. Server VAD's buffer also holds the second kept from before
# the detection and the silence that ends the turn; they do not count.
MAX_UTTERANCE_S = 30
# A transcript that is one bracketed tag is the recogniser naming a sound,
# not words: "<music>", "<BGM>". Punctuation alone is not words either.
_TAG_ONLY = re.compile(r"^\s*[<\[(][^<>\[\]()]*[>\])]\s*$")
_HAS_WORD = re.compile(r"[0-9A-Za-z\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
# Answer-row fields the socket passes on.
ANSWER_FIELDS = ("coverage_gap_s", "covered_through_s", "cumulative_count", "recalled_response_id", "recalled_seq")


def real_speech(text: str) -> bool:
    """Whether a transcript is words the user said."""
    return bool(text) and not _TAG_ONLY.match(text) and bool(_HAS_WORD.search(text))


def finite(value) -> float:
    """A message's number field as a float; ValueError when it is not a finite number."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(value) from None
    if not math.isfinite(number):
        raise ValueError(value)
    return number


def session_time(value) -> float:
    """A message's time field, bounded as an HTTP one is; ValueError when it is not one."""
    number = finite(value)
    if not 0 <= number <= MAX_T_S:
        raise ValueError(value)
    return number


async def run_audio(ws, runtime, transcriber, speaker, sid) -> None:
    """One audio socket, as docs/voice-protocol.md describes it.

    Microphone PCM comes up as binary frames and speech goes down the same
    way, bracketed by audio.begin and audio.end so the device knows where
    each sentence starts and ends. What gets spoken and what gets remembered
    is SpokenAnswers' decision; this only carries it out.
    """
    answers = SpokenAnswers()
    speech_of: dict[str, tuple[int, bool]] = {}   # speech_id -> (turn, is last)
    sent_ms: dict[int, float] = {}                # turn -> audio handed over
    speaking: asyncio.Task | None = None
    open_speech: str | None = None
    counter = 0

    async def send(message: dict) -> None:
        if ws.client_state is WebSocketState.CONNECTED:
            await ws.send_text(json.dumps(message, ensure_ascii=False))

    # --- the first message settles the formats -----------------------------
    try:
        hello = json.loads(await ws.receive_text())
    except Exception:
        await ws.close(code=4422, reason="first message must be hello")
        return
    if not isinstance(hello, dict) or hello.get("type") != "hello":
        await ws.close(code=4422, reason="first message must be hello")
        return
    try:
        in_rate = int(hello.get("rate", 16000))
        out_rate = int(hello.get("out_rate", in_rate))
        clock = session_time(hello.get("t", 0.0))
    except (TypeError, ValueError, OverflowError):
        await ws.close(code=4422, reason="rate out of range")
        return
    server_vad = hello.get("vad") == "server"
    if not (8000 <= in_rate <= 48000 and 8000 <= out_rate <= 48000):
        await ws.close(code=4422, reason="rate out of range")
        return
    vad = pcmlib.EnergyVAD(rate=in_rate) if server_vad else None
    _LOG.info("hello in=%d out=%d vad=%s t=%s", in_rate, out_rate, "server" if server_vad else "client", clock)
    await send({"type": "ready", "session_id": sid,
                "in_rate": in_rate, "out_rate": out_rate, "format": "s16le",
                "vad": "server" if server_vad else "client",
                "tts_rate": speaker.rate})

    utterance = bytearray()
    capturing = False   # client VAD opens it with speech.begin
    # Server VAD: the most the buffer may hold in this turn, set when it begins.
    turn_limit = MAX_UTTERANCE_S * in_rate * 2

    async def run(actions, *, playback_confirmed=False) -> None:
        nonlocal speaking, open_speech
        for action in actions:
            if isinstance(action, Speak):
                speaking = asyncio.create_task(say(action.turn, action.text))
            elif isinstance(action, Interrupt):
                if speaking is not None:
                    speaking.cancel()
                    await asyncio.gather(speaking, return_exceptions=True)
                    speaking = None
                if open_speech is not None:
                    await send({"type": "audio.cancel", "speech_id": open_speech})
                    open_speech = None
                answers.delivered(action.turn, sent_ms.get(action.turn, 0.0))
            elif isinstance(action, Commit):
                runtime.session.commit(action.response_id, action.text if playback_confirmed else "")

    async def say(turn: int, text: str) -> None:
        nonlocal counter, open_speech
        total_ms = 0.0
        spans = pcmlib.sentence_spans(text)
        for index, (part, end) in enumerate(spans):
            counter += 1
            speech_id = f"sp_{counter}"
            speech_of[speech_id] = (turn, index == len(spans) - 1)
            open_speech = speech_id
            await send({"type": "audio.begin", "speech_id": speech_id,
                        "rate": out_rate, "format": "s16le", "text": part})
            written = 0
            # The synthesizer's own rate goes out as it comes; any other is resampled here.
            resampler = Resampler(speaker.rate, out_rate) if out_rate != speaker.rate else None
            try:
                async for chunk in speaker(part):
                    data = resampler(chunk) if resampler else chunk
                    if data:
                        await ws.send_bytes(data)
                        written += len(data)
                if resampler and (data := resampler(b"", last=True)):
                    await ws.send_bytes(data)
                    written += len(data)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await send({"type": "error", "detail": f"tts: {error!r}", "fatal": False})
                return
            total_ms += written / 2 / out_rate * 1000
            sent_ms[turn] = total_ms
            # Where this sentence ends, in characters of the answer and in
            # milliseconds of audio: the pair an interruption resolves with.
            answers.progress(turn, end, total_ms)
            await send({"type": "audio.end", "speech_id": speech_id,
                        "samples": written // 2, "ms": round(total_ms)})
            open_speech = None

    # --- answers and captions arrive on the session's own stream -----------
    queue = runtime.subscribe()

    async def watch() -> None:
        while True:
            row = await queue.get()
            if row is None:
                return
            if row.get("type") == "caption":
                await send({"type": "caption", "response_id": row.get("response_id"),
                            "t": row.get("t"), "text": row.get("text")})
            elif row.get("type") == "answer":
                _LOG.info("answer t=%s %r", row.get("t"), row.get("text"))
                await send({"type": "answer", "response_id": row.get("response_id"),
                            "t": row.get("t"), "text": row.get("text"),
                            "status": row.get("status"), "route": row.get("route"), "task": row.get("task"),
                            **{key: row[key] for key in ANSWER_FIELDS if key in row}})
                await run(answers.answer(row["response_id"], row["text"]))
            elif row.get("type") == "error":
                await send({"type": "error", "detail": row.get("text"), "fatal": True})

    watcher = asyncio.create_task(watch())

    async def began(t: float | None) -> None:
        """The microphone opened. Stop speaking over the user - nothing else.

        A segment is not known to be the user until ASR has read it: on a
        wearable device one sentence arrived as four segments, two of them ASR
        noise and one empty. Cancelling the running answer here threw away the
        reply to the real question, so that waits for a real transcript.
        """
        nonlocal capturing
        capturing = True
        utterance.clear()
        await run(answers.user_paused())

    async def dropped(detail: str) -> None:
        """The turn being captured is thrown away; the socket goes on."""
        nonlocal capturing
        capturing = False
        utterance.clear()
        await send({"type": "error", "detail": detail, "fatal": False})
        await run(answers.user_stopped())

    async def ended(t: float | None) -> None:
        nonlocal capturing
        capturing = False
        audio, utterance[:] = bytes(utterance), b""
        when = clock if t is None else t
        ms = len(audio) / 2 / in_rate * 1000
        if len(audio) < MIN_UTTERANCE_MS * in_rate * 2 / 1000:
            # Too short to be a question. ASR invents a line for a fragment
            # of room noise, and that line would cancel the real answer.
            _LOG.info("utterance %.0fms too short, dropped", ms)
            await run(answers.user_stopped())
            return
        observation = runtime.session.observe(when) if runtime.offline_dir is None else None
        try:
            text = await transcriber(audio, in_rate)
        except Exception as error:
            _LOG.warning("asr failed after %.0fms: %r", ms, error)
            await send({"type": "error", "detail": f"asr: {error!r}", "fatal": False})
            await run(answers.user_stopped())
            return
        _LOG.info("utterance %.0fms -> %r", ms, text)
        await send({"type": "transcript", "t": when, "text": text})
        if not real_speech(text):
            # Nothing was said. Leave the running answer and the queue alone.
            _LOG.info("  not speech, dropped")
            await run(answers.user_stopped())
            return
        # Now it is a question: the answer being generated is for the old one.
        await run(answers.user_spoke())
        await run(answers.flush())
        try:
            runtime.session.submit(text, when if runtime.offline_dir is None else None,
                                   observation=observation)
            if runtime.diagnostics is not None:
                runtime.diagnostics.question(runtime.session.request, when, text, "voice")
            if runtime.offline_dir is not None:
                runtime.last_activity = time.monotonic()
        except ValueError:
            await send({"type": "error", "detail": runtime.say("invalid_question"), "fatal": False})
        await run(answers.user_stopped())

    try:
        while True:
            packet = await ws.receive()
            if packet["type"] == "websocket.disconnect":
                break
            if (data := packet.get("bytes")) is not None:
                clock += len(data) / 2 / in_rate
                if vad is not None:
                    utterance.extend(data)
                    for event in vad(data):
                        if event == "begin":
                            tail = bytes(utterance)
                            await began(clock)
                            # keep what the detector already heard
                            utterance.extend(tail[-in_rate * 2 :])
                            hangover = vad.hangover_ms * in_rate // 1000 * 2
                            turn_limit = len(utterance) + MAX_UTTERANCE_S * in_rate * 2 + hangover
                            await send({"type": "speech.detected", "t": clock})
                        elif capturing:
                            # (not after a turn dropped as too long: its end is no question)
                            await ended(clock)
                    if not capturing:
                        # Between turns only the second a detection would keep is kept: the
                        # microphone streams all the time.
                        del utterance[: -in_rate * 2]
                    elif len(utterance) > turn_limit:
                        await dropped(runtime.say("voice_too_long"))
                elif capturing:
                    if len(utterance) + len(data) > MAX_UTTERANCE_S * in_rate * 2:
                        await dropped(runtime.say("voice_too_long"))
                    else:
                        utterance.extend(data)
                continue
            if (text := packet.get("text")) is None:
                continue
            try:
                message = json.loads(text)
            except ValueError:
                await send({"type": "error", "detail": "not JSON", "fatal": False})
                continue
            if not isinstance(message, dict):
                await send({"type": "error", "detail": "not a JSON object", "fatal": False})
                continue
            kind = message.get("type")
            if kind == "speech.begin" and not server_vad:
                await began(message.get("t"))
            elif kind == "speech.end" and not server_vad:
                try:
                    t = None if message.get("t") is None else session_time(message["t"])
                except ValueError:
                    await dropped("t must be a number from 0 to 10^10")
                    continue
                await ended(t)
            elif kind == "play.done":
                turn, last = speech_of.pop(message.get("speech_id"), (None, False))
                if turn is not None and last:
                    await run(answers.spoken(turn), playback_confirmed=True)
            elif kind == "play.progress":
                pass
            elif kind == "cancel":
                try:
                    played_ms = finite(message.get("played_ms", 0.0))
                except ValueError:
                    await send({"type": "error", "detail": "played_ms must be a finite number", "fatal": False})
                    continue
                answers.heard(played_ms)
                await run(answers.flush(), playback_confirmed=True)
            elif kind == "bye":
                await send({"type": "closed"})
                break
            elif kind == "hello":
                await send({"type": "error", "detail": "already said hello",
                            "fatal": False})
    finally:
        watcher.cancel()
        pending = [watcher]
        if speaking is not None:
            speaking.cancel()
            pending.append(speaking)
        # gather(None) raises TypeError, which used to skip the unsubscribe
        # below and leave the session pushing rows into a dead queue.
        await asyncio.gather(*pending, return_exceptions=True)
        runtime.unsubscribe(queue)
        if ws.client_state is WebSocketState.CONNECTED:
            with contextlib.suppress(Exception):
                await ws.close()
