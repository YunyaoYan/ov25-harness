"""An uploaded recording: checked once, then sampled by time and decoded on request."""
import asyncio
from bisect import bisect_left, bisect_right
from contextlib import suppress
from dataclasses import dataclass, field
from fractions import Fraction
from itertools import pairwise
import json
import os
from pathlib import Path
import signal
from tempfile import TemporaryDirectory

MAX_DURATION_S = 1800
MAX_SOURCE_FRAMES = 108000
SUPPORTED_CODECS = {"h264", "hevc"}
# Decoding a whole recording at a steady rate: about 11 s for five minutes of 720p with four threads.
DECODE_THREADS = 4
DECODE_TIMEOUT_S = 600


class InvalidRecording(ValueError):
    pass


# ffmpeg executable -> the option that keeps every selected frame as it is.
_PASSTHROUGH = {}


async def media_command(*args, timeout=90):
    proc = await asyncio.create_subprocess_exec(
        *map(str, args), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout)
    finally:
        if proc.returncode is None:
            with suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.communicate()
    if proc.returncode:
        raise InvalidRecording("video preprocessing failed")
    return stdout


async def passthrough_option(ffmpeg):
    """-fps_mode from ffmpeg 5.1 on; older ones (4.4 is still common) know only -vsync."""
    if ffmpeg not in _PASSTHROUGH:
        usage = await media_command(ffmpeg, "-hide_banner", "-h", "long", timeout=10)
        _PASSTHROUGH[ffmpeg] = "-fps_mode" if b"-fps_mode" in usage else "-vsync"
    return _PASSTHROUGH[ffmpeg]


@dataclass(frozen=True)
class Recording:
    path: Path
    fps: float
    timestamps: tuple[float, ...]
    origin_pts: float = 0.
    ffmpeg: str = "ffmpeg"
    size: tuple[int, int] = (0, 0)
    # The decodes seconds() has started, by (rate, size): one per recording, however many questions.
    _decoded: dict = field(default_factory=dict, compare=False, repr=False)

    @property
    def end(self):
        return self.timestamps[-1]

    @classmethod
    async def inspect(cls, path, *, ffprobe="ffprobe", ffmpeg="ffmpeg", max_duration_s=MAX_DURATION_S):
        probe = (ffprobe, "-v", "error", "-f", "mov", "-protocol_whitelist", "file",
                 "-select_streams", "V:0", "-of", "json")
        raw = await media_command(*probe, "-show_entries",
                                  "stream=avg_frame_rate,time_base,width,height,codec_name,duration,nb_frames", path)
        try:
            stream, = json.loads(raw)["streams"]
            fps = float(Fraction(stream["avg_frame_rate"]))
            time_base = Fraction(stream["time_base"])
            duration = float(stream["duration"])
            declared_frames = int(stream.get("nb_frames", 0))
            if (not 0 < fps <= 120 or not 0 < duration <= max_duration_s or time_base <= 0
                    or declared_frames > MAX_SOURCE_FRAMES or duration * fps > MAX_SOURCE_FRAMES + 1
                    or not 0 < stream["width"] * stream["height"] <= 4096 * 2160
                    or stream["codec_name"] not in SUPPORTED_CODECS):
                raise ValueError("video metadata outside the supported recording profile")
            raw = await media_command(*probe, "-threads", "2", "-show_frames", "-show_entries",
                                      "frame=best_effort_timestamp", path)
            ticks = [int(frame["best_effort_timestamp"]) for frame in json.loads(raw)["frames"]]
            if not 1 <= len(ticks) <= MAX_SOURCE_FRAMES:
                raise ValueError("missing or excessive frame timestamps")
            timestamps = tuple(float((tick - ticks[0]) * time_base) for tick in ticks)
            tolerance = max(.003, 2 * float(time_base))
            if (any(a >= b for a, b in pairwise(timestamps))
                    or any(abs(pts - index / fps) > tolerance for index, pts in enumerate(timestamps))):
                raise ValueError("the native renderer requires a constant source frame rate")
        except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError) as error:
            raise InvalidRecording("expected a constant-frame-rate H.264 or HEVC MP4/MOV recording") from error
        return cls(path, fps, timestamps, float(ticks[0] * time_base), ffmpeg, (stream["width"], stream["height"]))

    def seconds(self, rate, size):
        """The recording as a video: a frame every 1/rate seconds, scaled to size, each with its
        timestamp. Decoded once and shared; the task gives [dict(png=<jpeg>, pts, timestamp, ...)]."""
        key = (rate, tuple(size))
        task = self._decoded.get(key)
        if task is None or task.cancelled() or task.done() and task.exception() is not None:
            task = self._decoded[key] = asyncio.create_task(self._decode_seconds(rate, tuple(size)))
        return task

    async def forget(self):
        """Stop any decode still running and wait for it: the recording's folder is about to go."""
        tasks = list(self._decoded.values())
        self._decoded.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _decode_seconds(self, rate, size):
        passthrough = await passthrough_option(self.ffmpeg)
        with TemporaryDirectory(prefix="ov25-seconds-", dir=self.path.parent) as directory:
            # The fps filter counts from the first frame, as the timestamps do; frame k is k/rate seconds.
            # eof_action=pass keeps the last second, which the filter otherwise drops.
            await media_command(
                self.ffmpeg, "-nostdin", "-v", "error", "-f", "mov", "-protocol_whitelist", "file",
                "-threads", DECODE_THREADS, "-noautorotate", "-i", self.path, "-map", "0:V:0",
                "-vf", f"fps={rate:.6f}:round=near:eof_action=pass,scale={size[0]}:{size[1]}",
                passthrough, "passthrough", "-q:v", "4", Path(directory) / "%05d.jpg", timeout=DECODE_TIMEOUT_S,
            )
            frames = await asyncio.to_thread(self._read_seconds, Path(directory), rate)
        if not frames:
            raise InvalidRecording("no frames decoded")
        return frames

    def _read_seconds(self, directory, rate):
        frames = []
        for k, image in enumerate(sorted(directory.glob("*.jpg"))):
            pts = k / rate
            if pts > self.end + 1e-6:
                break
            frames.append(dict(png=image.read_bytes(), pts=pts, timestamp=pts, fps=self.fps,
                               frame_index=min(round(pts * self.fps), len(self.timestamps) - 1)))
        return frames

    def indices(self, start, end, budget):
        first, stop = bisect_left(self.timestamps, start), bisect_right(self.timestamps, end)
        count = min(budget, stop - first)
        if count <= 0:
            return []
        if count == 1:
            return [stop - 1]
        return [first + round(i * (stop - first - 1) / (count - 1)) for i in range(count)]

    async def rgb(self, indices, pixel_budget):
        with TemporaryDirectory(prefix="ov25-offline-rgb-") as directory:
            root = Path(directory)
            # Seek once per small group. Select by the original presentation
            # clock so B-frames and nonzero container starts keep their identity.
            groups = []
            for index in indices:
                if not groups or self.timestamps[index] - self.timestamps[groups[-1][0]] > 2:
                    groups.append([])
                groups[-1].append(index)
            decoder_slots = asyncio.Semaphore(2)
            passthrough = await passthrough_option(self.ffmpeg)

            async def decode(group):
                async with decoder_slots:
                    times = [self.origin_pts + self.timestamps[index] for index in group]
                    epsilon = min(.001, .125 / self.fps)
                    select = "+".join(f"between(t\\,{t-epsilon:.9f}\\,{t+epsilon:.9f})" for t in times)
                    pattern = root / f"{group[0]}-%03d.png"
                    await media_command(
                        self.ffmpeg, "-nostdin", "-v", "error", "-f", "mov", "-protocol_whitelist", "file",
                        "-copyts", "-ss", max(0., self.timestamps[group[0]] - .5),
                        "-threads", "1", "-noautorotate", "-i", self.path, "-map", "0:V:0", "-filter_threads", "1",
                        "-vf", f"select={select},scale=w='min(iw,768)':h='min(ih,768)':force_original_aspect_ratio=decrease",
                        passthrough, "passthrough", "-frames:v", len(group), "-threads", "1", pattern, timeout=30,
                    )
                    # A few small pictures from a local temporary folder: not worth a thread.
                    images = sorted(root.glob(f"{group[0]}-*.png"))  # noqa: ASYNC240
                    if len(images) != len(group):
                        raise InvalidRecording("decoded frames do not match the requested sample")
                    return [dict(png=image.read_bytes(), pts=self.timestamps[index], frame_index=index,
                                 fps=self.fps, pixel_budget=pixel_budget)
                            for index, image in zip(group, images, strict=True)]

            decoders = [asyncio.create_task(decode(group)) for group in groups]
            try:
                batches = await asyncio.wait_for(asyncio.gather(*decoders), 34)
            finally:
                for decoder in decoders:
                    decoder.cancel()
                await asyncio.gather(*decoders, return_exceptions=True)
            return [frame for batch in batches for frame in batch]
