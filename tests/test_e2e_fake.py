"""The whole service on the fake backends, driven over HTTP by ov25_harness.check and the quickstart.

Starts `ov25-harness serve --config configs/local-fake.toml` on a free loopback port, so it needs
nothing but the package's own dependencies (and ffmpeg/ffprobe on PATH for the recording checks,
which are skipped without them). Takes about two minutes.
"""
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

import httpx

ROOT = Path(__file__).resolve().parents[1]
IMAGE = ROOT / "tests" / "data" / "frame.jpg"


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class FakeServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = dict(os.environ, PYTHONPATH=os.pathsep.join(
            filter(None, [str(ROOT / "src"), os.environ.get("PYTHONPATH")])))
        cls.url = f"http://127.0.0.1:{free_port()}"
        cls.log = tempfile.TemporaryFile()
        cls.server = subprocess.Popen(
            [sys.executable, "-m", "ov25_harness.cli", "serve", "--config", str(ROOT / "configs" / "local-fake.toml"),
             "--port", cls.url.rsplit(":", 1)[1]], cwd=ROOT, env=cls.env, stdout=cls.log, stderr=subprocess.STDOUT)
        end = time.monotonic() + 30
        while time.monotonic() < end and cls.server.poll() is None:
            try:
                if httpx.get(cls.url + "/healthz", timeout=1, trust_env=False).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(.2)
        cls.tearDownClass()
        raise RuntimeError("the fake service did not come up")

    @classmethod
    def tearDownClass(cls):
        if cls.server.poll() is None:
            cls.server.terminate()
            try:
                cls.server.wait(10)
            except subprocess.TimeoutExpired:
                cls.server.kill()
                cls.server.wait()
        cls.log.seek(0)
        cls.server_log = cls.log.read().decode("utf-8", "replace")
        cls.log.close()

    def run_client(self, *args, timeout=300):
        done = subprocess.run([sys.executable, *args], cwd=ROOT, env=self.env, capture_output=True, text=True,
                              timeout=timeout)
        return done.returncode, done.stdout + done.stderr

    def check(self, *flags):
        code, output = self.run_client("-m", "ov25_harness.cli", "check", self.url, str(IMAGE), *flags)
        self.assertEqual(code, 0, output)
        self.assertIn("ALL OK", output)
        self.assertNotIn("FAIL", output)
        return output

    def test_check_english(self):
        recording = shutil.which("ffmpeg") and shutil.which("ffprobe")
        output = self.check("--language", "en", *(() if recording else ("--skip-recording",)))
        if recording:
            self.assertIn("a question about the recording is answered", output)
        self.assertIn("sessions open afterwards: 0", output)

    def test_check_chinese(self):
        self.check("--language", "zh", "--skip-recording")

    def test_quickstart(self):
        quickstart = ROOT / "examples" / "quickstart.py"
        if not quickstart.exists():
            self.skipTest("no examples/quickstart.py")
        code, output = self.run_client(str(quickstart), "--url", self.url, "--image", str(IMAGE), timeout=120)
        self.assertEqual(code, 0, output)
        self.assertIn("answer [ok, route realtime", output)


if __name__ == "__main__":
    unittest.main()
