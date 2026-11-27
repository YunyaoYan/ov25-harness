"""An uploaded recording decoded by whichever ffmpeg the machine has."""
import asyncio
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from ov25_harness import recording as module

DATA = Path(__file__).resolve().parent / "data"


class PassthroughTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_ffmpeg_before_5_1_gets_vsync(self):
        # Old logic: -fps_mode always. ffmpeg 4.4 (Ubuntu 22.04's) refuses it, so every question about
        # an uploaded recording failed with upstream_unavailable.
        with tempfile.TemporaryDirectory() as folder:
            for name, usage, expected in (
                    ("ffmpeg-4.4", "-vsync <>  video sync method", "-vsync"),
                    ("ffmpeg-8.1", "-vsync <>  deprecated, use -fps_mode\n-fps_mode[:<stream_spec>]  framerate mode",
                     "-fps_mode")):
                path = Path(folder) / name
                path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{usage}'\n")
                path.chmod(0o755)
                self.assertEqual(await module.passthrough_option(str(path)), expected, name)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg and ffprobe")
    async def test_the_ffmpeg_on_the_path_decodes_the_requested_frames(self):
        with tempfile.TemporaryDirectory() as folder:
            clip = Path(folder) / "clip.mp4"
            await asyncio.to_thread(subprocess.run, [
                "ffmpeg", "-v", "error", "-y", "-loop", "1", "-i", str(DATA / "frame.jpg"), "-t", "2", "-r", "5",
                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)],
                check=True)
            recording = await module.Recording.inspect(clip)
            self.assertEqual(len(recording.timestamps), 10)
            frames = await recording.rgb([0, 5, 9], 331776)
        self.assertEqual([frame["frame_index"] for frame in frames], [0, 5, 9])
        self.assertTrue(all(frame["png"].startswith(b"\x89PNG") for frame in frames))


if __name__ == "__main__":
    unittest.main()
