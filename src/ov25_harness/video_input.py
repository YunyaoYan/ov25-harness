"""A whole recording to the offline model as one video, with as many frames as its context holds."""
import asyncio
import base64
from contextlib import suppress
import json
import math
import os
from uuid import uuid4

# The offline model's --max-model-len and visual encoder limit come from [chat] context_tokens and
# encoder_tokens. A server with other limits answers 400 and the frames are halved.
MAX_FRAMES = 32
ANSWER_TOKENS = 256
_CAPACITY = ("encoder cache size", "maximum context length", "max_model_len", "maximum model length",
             "context window", "maximum input length", "maximum context size")


class NoRoom(Exception):
    """Not even one frame of this recording fits next to the question."""


async def frame_tokens(path, ffprobe="ffprobe"):
    """An upper bound on the tokens of one frame: 16-pixel patches merged 3x3, plus its timestamp."""
    proc = await asyncio.create_subprocess_exec(
        ffprobe, "-v", "error", "-protocol_whitelist", "file", "-f", "mov", "-select_streams", "V:0",
        "-show_entries", "stream=width,height", "-of", "json", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        raw, _ = await proc.communicate()
    finally:
        if proc.returncode is None:
            with suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
    try:
        stream, = json.loads(raw)["streams"]
        width, height = int(stream["width"]), int(stream["height"])
        if min(width, height) < 1:
            raise ValueError("invalid dimensions")
    except (ValueError, KeyError, TypeError):
        return 9000   # the largest picture the app accepts
    return math.ceil(width / 48) * math.ceil(height / 48) + 16


async def post(client, url, model, path, question, frames):
    """One chat request with the file as a video_url, read and encoded a piece at a time."""
    marker = uuid4().hex
    mime = "video/quicktime" if path.suffix.lower() == ".mov" else "video/mp4"
    payload = dict(model=model, temperature=0, max_tokens=ANSWER_TOKENS,
                   media_io_kwargs={"video": {"num_frames": frames}},
                   messages=[{"role": "user", "content": [
                       {"type": "video_url", "video_url": {"url": f"data:{mime};base64,{marker}"}},
                       {"type": "text", "text": question}]}])
    prefix, suffix = json.dumps(payload).encode().split(marker.encode())
    with path.open("rb") as source:
        size = os.fstat(source.fileno()).st_size
        length = len(prefix) + 4 * ((size + 2) // 3) + len(suffix)

        async def content():
            yield prefix
            # Whole base64 triplets per piece, so the pieces concatenate without padding.
            while chunk := await asyncio.to_thread(source.read, 3 * 256 * 1024):
                yield base64.b64encode(chunk)
            yield suffix

        return await client.post(url, content=content(), headers={
            "Content-Type": "application/json", "Content-Length": str(length)})


async def ask(client, url, model, path, question, *, context_tokens=12288, encoder_tokens=11552, ffprobe="ffprobe"):
    """The model's answer about the whole file: (text, finish_reason, frames it was given)."""
    # A UTF-8 byte is at least one token's worth of room for the question.
    room = context_tokens - ANSWER_TOKENS - len(question.encode()) - 256
    per_frame = await frame_tokens(path, ffprobe)
    frames = min(MAX_FRAMES, min(encoder_tokens, room) // per_frame)
    while frames >= 1:
        response = await post(client, url, model, path, question, frames)
        if response.status_code == 400 and any(word in response.text.lower() for word in _CAPACITY):
            frames //= 2
            continue
        response.raise_for_status()
        choice = response.json()["choices"][0]
        return choice["message"]["content"] or "", choice.get("finish_reason"), frames
    raise NoRoom
