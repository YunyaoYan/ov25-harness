# SPDX-License-Identifier: Apache-2.0
# Adapted from the earlier OVStreaming voice implementation. See NOTICE for attribution.
# Modified for ov25-harness: moved resampling out of this module, disabled environment
# proxy settings, and updated punctuation and device wording.
"""Raw PCM in, raw PCM out: the voice path a device can speak over a socket.

A small device has neither WebRTC nor a browser - only a byte stream of
microphone PCM and a speaker that wants PCM back. This module is that path
with no transport in it: speech recognition, speech synthesis, sentence
splitting and a voice detector, each reachable on its own. Which answers get
spoken and how much of each is remembered stays in voice/answers.py, and the
socket itself stays in the caller.

Everything here speaks 16-bit little-endian mono, the one format both ends of
the device link use.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
import io
import re
import wave

import httpx

# Sentence ends, Chinese and English, kept with the sentence they close. A
# decimal point or an abbreviation would split an English sentence early; the
# answers are short enough that the cost of that is one extra TTS request.
_SENTENCE_END = re.compile(r"(?<=[\u3002\uff0e\uff01\uff1f!?\uff1b;])|(?<=[.])(?=\s)|(?<=\n)")


def sentences(text: str) -> list[str]:
    """Split an answer into the pieces that are synthesised one at a time."""
    parts = [part.strip() for part in _SENTENCE_END.split(text)]
    return [part for part in parts if part]


def sentence_spans(text: str) -> list[tuple[str, int]]:
    """Each sentence with where it ends in the original text.

    The offsets are what gets committed as memory after an interruption, so
    they have to index the answer the model wrote, not the stripped pieces
    that went to the speech server.
    """
    spans, offset = [], 0
    for part in sentences(text):
        start = text.index(part, offset)
        offset = start + len(part)
        spans.append((part, offset))
    return spans


def to_wav(pcm: bytes, rate: int) -> bytes:
    """Wrap s16le mono audio in a WAV container, which is what ASR accepts."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)
    return buffer.getvalue()


class Transcriber:
    """One utterance at a time, through an OpenAI-compatible transcription API.

    No language is sent. Qwen3-ASR transcribes as whatever language it is
    told, so naming one makes it mis-transcribe everything else; without one
    it detects the language itself. The response format is json because vLLM
    serves verbose_json only for models that have segment timestamps.
    """

    def __init__(self, base_url: str, model: str, *, timeout: float = 120.0) -> None:
        self.model = model
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout, trust_env=False)

    async def __call__(self, pcm: bytes, rate: int) -> str:
        if not pcm:
            return ""
        response = await self._http.post(
            "/audio/transcriptions",
            files={"file": ("utterance.wav", to_wav(pcm, rate), "audio/wav")},
            data={"model": self.model, "response_format": "json"},
        )
        response.raise_for_status()
        return (response.json().get("text") or "").strip()

    async def aclose(self) -> None:
        await self._http.aclose()


class Speaker:
    """A sentence at a time, through an OpenAI-compatible speech API.

    The server streams PCM as it decodes, so the first chunk arrives long
    before the sentence is finished; the caller forwards each chunk as it
    comes and the device starts playing at the first one.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        voice: str,
        *,
        rate: int = 24000,
        timeout: float = 300.0,
    ) -> None:
        self.model = model
        self.voice = voice
        self.rate = rate
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout, trust_env=False)

    async def __call__(self, text: str, *, chunk: int = 8192) -> AsyncIterator[bytes]:
        payload = {
            "model": self.model,
            "input": text,
            "voice": self.voice,
            "response_format": "pcm",
            "stream_format": "audio",
        }
        async with self._http.stream("POST", "/audio/speech", json=payload) as response:
            response.raise_for_status()
            async for data in response.aiter_bytes(chunk):
                if data:
                    yield data

    async def aclose(self) -> None:
        await self._http.aclose()


@dataclass
class EnergyVAD:
    """Speech detection for a close-talking microphone.

    A head-worn device holds the microphone a few centimetres from the mouth,
    so speech sits far above the room. This tracks the quietest recent frame
    as the noise floor and calls anything well above it speech, with a
    hangover so a pause inside a sentence does not end the utterance. It is
    not Silero: in a noisy room it will cut late. Prefer client VAD where the
    device has one.
    """

    rate: int = 16000
    frame_ms: int = 20
    # How far above the noise floor a frame has to be to count as speech.
    threshold: float = 4.0
    # Silence that ends an utterance. A question cannot be taken back once it
    # is appended, so this is long enough to sit through a breath.
    hangover_ms: int = 800
    floor: float = 200.0

    def __post_init__(self) -> None:
        self._tail = b""
        self._speaking = False
        self._quiet_ms = 0

    @property
    def frame_bytes(self) -> int:
        return int(self.rate * self.frame_ms / 1000) * 2

    def __call__(self, pcm: bytes) -> list[str]:
        """Feed audio, get back any of "begin" and "end" that it crossed."""
        import numpy as np

        events: list[str] = []
        data = self._tail + pcm
        size = self.frame_bytes
        whole = len(data) // size * size
        self._tail = data[whole:]
        for start in range(0, whole, size):
            samples = np.frombuffer(data[start : start + size], dtype="<i2")
            level = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)) + 1e-6)
            if level < self.floor or self.floor == 0:
                # Track the quietest thing heard lately, drifting up slowly so
                # a long silence does not make the floor unreachably low.
                self.floor = level
            else:
                self.floor += (level - self.floor) * 0.001
            loud = level > self.floor * self.threshold
            if loud:
                self._quiet_ms = 0
                if not self._speaking:
                    self._speaking = True
                    events.append("begin")
            elif self._speaking:
                self._quiet_ms += self.frame_ms
                if self._quiet_ms >= self.hangover_ms:
                    self._speaking = False
                    self._quiet_ms = 0
                    events.append("end")
        return events
