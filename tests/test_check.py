"""The check command's options."""
from pathlib import Path
import unittest
from unittest.mock import patch

from ov25_harness import check

IMAGE = Path(__file__).resolve().parent / "data" / "frame.jpg"


class CommandTests(unittest.TestCase):
    def test_the_idle_close_check_waits_out_the_services_own_limit(self):
        # Old logic: 20 s whatever the service's live_idle_s, so a service with live_idle_s = 30 failed.
        seen = {}

        async def run_checks(*args, **options):
            seen.update(options)
            return []

        with patch.object(check, "run_checks", run_checks):
            self.assertEqual(check.main(["http://127.0.0.1:1", str(IMAGE), "--live-idle-s", "30"]), 0)
            self.assertEqual(seen["live_idle_s"], 30.)
            check.main(["http://127.0.0.1:1", str(IMAGE)])
            self.assertEqual(seen["live_idle_s"], 10.)
        self.assertEqual(check.Checker("http://x", b"", "en", 30.).live_idle_s, 30.)


if __name__ == "__main__":
    unittest.main()
