"""The service's settings: one TOML file, checked once at start.

Every section and key is optional and has the default below. An unknown key is an error, and every
limit has a hard ceiling, so a mistyped or out-of-range value stops the service at start with the
field's name instead of being ignored. Secrets are never in the file: ``api_key_env`` names the
environment variable that holds a key. See docs/configuration.md.
"""
from pathlib import Path
import tomllib
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import locale

# A source compacted to more than this many frames stalls on every compaction for too long.
MAX_COMPACT_TO_VISUAL_FRAMES = 100


def _url(value, *, empty=False):
    """An http(s) URL with a host and nothing a credential could hide in."""
    if empty and value == "":
        return value
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError("must be an http(s) URL without credentials, query or fragment")
    return value.rstrip("/")


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Server(_Section):
    # Must be set explicitly to listen beyond loopback.
    host: str = "127.0.0.1"
    port: int = Field(8100, ge=1, le=65535)
    # Pixel budget passed to the models with each frame.
    max_pixels: int = Field(331776, ge=1, le=2_000_000)
    # Locales whose rules are active; a reply follows the language of its question.
    languages: list[str] = Field(default_factory=lambda: ["en"], min_length=1, max_length=8)
    # Empty: log to stderr only; else a daily-rotated file in this directory, kept log_keep_days.
    log_dir: str = ""
    log_keep_days: int = Field(14, ge=1, le=3650)

    @field_validator("languages")
    @classmethod
    def _languages(cls, value):
        known = locale.available()
        if len(set(value)) != len(value) or not set(value) <= set(known):
            raise ValueError(f"must be distinct languages out of {known}")
        return value


class Streaming(_Section):
    kind: Literal["ov-streaming", "fake"] = "ov-streaming"
    url: str = "http://127.0.0.1:8011"
    # Live sessions at once; each holds one engine session.
    user_streams: int = Field(4, ge=0, le=256)
    # Frames a source keeps before it is compacted, and what it is compacted to, when the client
    # does not ask. Not a preference: an unbounded source lets one long session stall the engine.
    max_visual_frames: int = Field(150, ge=1, le=4096)
    compact_to_visual_frames: int = Field(75, ge=1, le=MAX_COMPACT_TO_VISUAL_FRAMES)
    # The most a client may ask for.
    max_visual_frames_limit: int = Field(700, ge=1, le=4096)
    # Frames sent to the source and not yet decided before the source is dropped. A compaction
    # that keeps 100 frames stalls the source ~2.3 s, so at 1 frame a second 8 rides out about 7 s.
    frame_backlog: int = Field(8, ge=1, le=64)

    @field_validator("url")
    @classmethod
    def _check_url(cls, value):
        return _url(value)

    @model_validator(mode="after")
    def _frames(self):
        if not self.compact_to_visual_frames <= self.max_visual_frames <= self.max_visual_frames_limit:
            raise ValueError("need compact_to_visual_frames <= max_visual_frames <= max_visual_frames_limit")
        return self


class Chat(_Section):
    """The offline model, behind an OpenAI-compatible /v1/chat/completions."""
    kind: Literal["openai", "fake"] = "openai"
    url: str = "http://127.0.0.1:8000/v1"
    model: str = Field("ov25-4b-instruct", min_length=1)
    max_concurrency: int = Field(8, ge=1, le=256)
    # Images per request; must match the server's --limit-mm-per-prompt.
    max_images: int = Field(8, ge=1, le=64)
    # The server's --max-model-len and visual encoder limit, for the frame budget of a whole video.
    context_tokens: int = Field(12288, ge=1024, le=1_048_576)
    encoder_tokens: int = Field(11552, ge=1, le=1_048_576)

    @field_validator("url")
    @classmethod
    def _check_url(cls, value):
        return _url(value)


class Voice(_Section):
    enabled: bool = False
    asr_url: str = "http://127.0.0.1:8002/v1"
    asr_model: str = "Qwen/Qwen3-ASR-1.7B"
    tts_url: str = "http://127.0.0.1:8091/v1"
    tts_model: str = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
    tts_voice: str = "vivian"
    tts_rate: int = Field(24000, ge=8000, le=48000)

    @field_validator("asr_url", "tts_url")
    @classmethod
    def _check_url(cls, value):
        return _url(value)


class Subtask(_Section):
    # "" = DirectSubtask; else "package.module:factory", e.g. "ov25_harness.rewrite:factory".
    factory: str = Field("", pattern=r"^(?:[A-Za-z_][\w.]*:[A-Za-z_]\w*)?$")


class Rewrite(_Section):
    """Used only by ov25_harness.rewrite, which sends the question text to this service."""
    enabled: bool = False
    url: str = ""
    model: str = ""
    # The environment variable holding the key; empty sends no Authorization header.
    api_key_env: str = Field("", pattern=r"^(?:[A-Za-z_][A-Za-z0-9_]*)?$")
    timeout_s: float = Field(3., gt=0, le=30)

    @field_validator("url")
    @classmethod
    def _check_url(cls, value):
        return _url(value, empty=True)


class Media(_Section):
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    max_upload_bytes: int = Field(128 * 1024 * 1024, ge=1, le=1024 ** 3)
    max_recording_s: float = Field(1800, gt=0, le=3600)
    max_offline_sessions: int = Field(4, ge=0, le=64)


class Policy(_Section):
    # A question has this long from submission to answer.
    live_turn_s: float = Field(14., gt=0, le=120)
    offline_turn_s: float = Field(145., gt=0, le=600)
    # Routing gets a short limit of its own because a late route still has an answer to wait for.
    route_timeout_s: float = Field(2., gt=0, le=30)
    # The newest frame answers a question about "now" only when it is this recent.
    current_frame_age_s: float = Field(2.5, gt=0, le=60)
    # Close a live session after this long without a frame, an offline one without a request.
    live_idle_s: float = Field(10., gt=0, le=3600)
    offline_idle_s: float = Field(600., gt=0, le=86400)
    max_frame_bytes: int = Field(2 * 1024 * 1024, ge=1, le=8 * 1024 * 1024)
    # Frames kept per session for current-frame answers, the past fallback and subtasks.
    buffer_frames: int = Field(90, ge=1, le=1000)
    buffer_bytes: int = Field(12 * 1024 * 1024, ge=1, le=256 * 1024 * 1024)
    # (question, answer) pairs a question without a picture sees.
    dialogue_turns: int = Field(6, ge=0, le=64)


class Settings(_Section):
    server: Server = Server()
    streaming: Streaming = Streaming()
    chat: Chat = Chat()
    voice: Voice = Voice()
    subtask: Subtask = Subtask()
    rewrite: Rewrite = Rewrite()
    media: Media = Media()
    policy: Policy = Policy()

    @model_validator(mode="after")
    def _rewrite(self):
        if self.rewrite.enabled and not (self.rewrite.url and self.rewrite.model):
            raise ValueError("rewrite.enabled needs rewrite.url and rewrite.model")
        return self


def load(path):
    """The settings in a TOML file; raises pydantic.ValidationError naming the field at fault."""
    with Path(path).open("rb") as handle:
        return Settings.model_validate(tomllib.load(handle))
