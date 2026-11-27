"""The services behind the app, built from the settings.

    chat        the offline model: async complete(body, timeout) -> (text, finish_reason)
    streaming   the streaming engine: session(**options) -> an ov_streaming ClientSession
    transcriber async (pcm, rate) -> text           (voice only)
    speaker     async iterator of PCM for a sentence (voice only)

``kind = "fake"`` swaps in the in-process backends of ov25_harness.fake: no GPU, no network.
"""
import asyncio

import httpx

from . import video_input


class OpenAIChat:
    """An OpenAI-compatible /chat/completions endpoint, at most max_concurrency calls at once."""

    def __init__(self, settings):
        chat = settings.chat
        self.settings = settings
        self.model = chat.model
        self.url = chat.url.rstrip("/") + "/chat/completions"
        # complete() bounds each call by the turn's deadline; httpx's own 5 s default would cut a
        # slow answer short.
        self.http = httpx.AsyncClient(trust_env=False, timeout=None, limits=httpx.Limits(max_connections=32))
        self.slots = asyncio.Semaphore(chat.max_concurrency)

    async def complete(self, body, timeout):
        async with asyncio.timeout(timeout), self.slots:
            response = await self.http.post(self.url, json=body)
            response.raise_for_status()
            choice = response.json()["choices"][0]
        return choice["message"]["content"] or "", choice.get("finish_reason")

    async def video(self, path, question, timeout):
        """A whole recording as one video: (text, finish_reason, frames it was given)."""
        chat, media = self.settings.chat, self.settings.media
        async with asyncio.timeout(timeout), self.slots:
            return await video_input.ask(self.http, self.url, self.model, path, question,
                                         context_tokens=chat.context_tokens, encoder_tokens=chat.encoder_tokens,
                                         ffprobe=media.ffprobe)

    async def aclose(self):
        await self.http.aclose()


def make_backends(settings):
    """(chat, streaming, transcriber, speaker) for the settings; the voice pair is None without voice."""
    if settings.chat.kind == "fake":
        from .fake import FakeChat

        chat = FakeChat(settings)
    else:
        chat = OpenAIChat(settings)
    if settings.streaming.kind == "fake":
        from .fake import FakeStreaming

        streaming = FakeStreaming(settings)
    else:
        from ov_streaming.client import OVClient

        streaming = OVClient(settings.streaming.url, timeout=600.0)
    transcriber = speaker = None
    if settings.voice.enabled:
        from .voice import pcm

        voice = settings.voice
        transcriber = pcm.Transcriber(voice.asr_url, voice.asr_model)
        speaker = pcm.Speaker(voice.tts_url, voice.tts_model, voice.tts_voice,
                              rate=voice.tts_rate)
    return chat, streaming, transcriber, speaker
