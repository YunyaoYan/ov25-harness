import unittest

from ov25_harness import config
from ov25_harness.frame_health import DELAYED, LOW, OK, FrameHealth

from .support import LOCALE


def chinese(key, **fields):
    return LOCALE.message("zh", key, **fields)


class FrameHealthTests(unittest.TestCase):
    def feed(self, health, frames):
        for t, arrived in frames:
            health.frame(t, arrived)

    def test_one_frame_a_second_is_ok_and_says_nothing(self):
        health = FrameHealth(now=100.)
        self.feed(health, [(float(i), 100.3 + i) for i in range(30)])
        self.assertEqual(health.summary(129.4), dict(status=OK, frames_10s=10, delay_s=0.))
        self.assertIsNone(health.warning(129.4))

    def test_a_young_session_is_not_judged_before_the_window_fills(self):
        health = FrameHealth(now=100.)
        self.feed(health, [(0., 100.), (1., 101.)])
        self.assertEqual(health.status(101.), OK)

    def test_sparse_frames_are_a_low_frame_rate_and_warned_once(self):
        health = FrameHealth(now=100., say=chinese)
        self.feed(health, [(3. * i, 100. + 3 * i) for i in range(8)])
        row = health.warning(121.)
        self.assertEqual((row["type"], row["status"], row["frames_10s"], row["t"]), ("warning", LOW, 4, 21.))
        self.assertEqual(row["text"], chinese("low_frame_rate", window="10", frames=4))
        self.assertIsNone(health.warning(122.))
        self.assertIsNone(health.warning(140.))

    def test_frames_arriving_later_than_their_timestamps_are_a_slow_network(self):
        health = FrameHealth(now=100., say=chinese)
        # Captured every second, each taking half a second longer to arrive than the one before.
        self.feed(health, [(float(i), 100. + 1.5 * i) for i in range(8)])
        self.assertEqual(health.status(110.5), DELAYED)
        row = health.warning(110.5)
        self.assertEqual((row["status"], row["delay_s"]), (DELAYED, 3.5))
        self.assertEqual(row["text"], chinese("network_delay", delay="4"))

    def test_without_a_locale_the_warning_is_in_english(self):
        health = FrameHealth(now=100.)
        self.feed(health, [(3. * i, 100. + 3 * i) for i in range(8)])
        self.assertEqual(health.warning(121.)["text"], LOCALE.message("en", "low_frame_rate", window="10", frames=4))
        self.assertIsNone(health.warning(121.))

    def test_it_warns_again_after_recovering(self):
        health = FrameHealth(now=0.)
        self.feed(health, [(3. * i, 3. * i) for i in range(6)])
        self.assertIsNotNone(health.warning(15.))
        self.feed(health, [(16. + i, 16. + i) for i in range(12)])
        self.assertIsNone(health.warning(27.))
        self.feed(health, [(30., 30.), (34., 34.), (38., 38.)])
        self.assertEqual(health.warning(38.)["status"], LOW)

    def test_idle_counts_from_the_last_frame_or_from_opening(self):
        health = FrameHealth(now=100.)
        self.assertEqual(health.idle_s(104.), 4.)
        health.frame(0., 105.)
        self.assertEqual(health.idle_s(107.), 2.)
        # The app closes a live session idle this long; the limit is configuration now.
        self.assertEqual(config.Settings().policy.live_idle_s, 10.)


if __name__ == "__main__":
    unittest.main()
