import math
import unittest

import numpy as np

from ov25_harness.resample import Resampler


def tone(hz, rate, seconds=1.0, level=12000):
    t = np.arange(int(rate * seconds)) / rate
    return (level * np.sin(2 * np.pi * hz * t)).astype("<i2")


def run(pcm, src, dst, sizes=(8192,)):
    """Feed the bytes in pieces of the given sizes, repeating the last size, then end the stream."""
    resampler, out, at, sizes = Resampler(src, dst), b"", 0, list(sizes)
    while at < len(pcm):
        size = sizes.pop(0) if len(sizes) > 1 else sizes[0]
        out += resampler(pcm[at:at + size])
        at += size
    return np.frombuffer(out + resampler(b"", last=True), "<i2").astype(np.float64)


def level_at(samples, hz, rate):
    """Amplitude of one frequency in the middle of the signal."""
    middle = samples[len(samples) // 4: 3 * len(samples) // 4]
    t = np.arange(len(middle)) / rate
    return 2 * abs(np.mean(middle * np.exp(-2j * np.pi * hz * t)))


class ResamplerTests(unittest.TestCase):
    def test_a_tone_keeps_its_pitch_level_and_length(self):
        for dst in (16000, 8000, 48000, 44100, 22050):
            out = run(tone(1000, 24000).tobytes(), 24000, dst)
            self.assertEqual(len(out), math.ceil(24000 * dst / 24000), dst)
            self.assertAlmostEqual(level_at(out, 1000, dst) / 12000, 1.0, delta=.01, msg=dst)
            ideal = 12000 * np.sin(2 * np.pi * 1000 * np.arange(len(out)) / dst)
            middle = slice(len(out) // 4, 3 * len(out) // 4)
            self.assertLess(np.abs(out[middle] - ideal[middle]).max(), 60, dst)

    def test_the_output_does_not_depend_on_how_the_stream_was_cut(self):
        rng = np.random.default_rng(0)
        pcm = (tone(440, 24000) // 2 + rng.integers(-3000, 3000, 24000)).astype("<i2").tobytes()
        whole = run(pcm, 24000, 16000, (len(pcm),))
        for sizes in ((8192,), (2,), (4096, 10, 30000, 6), (3, 5, 7, 1001)):      # odd sizes split a sample
            np.testing.assert_array_equal(run(pcm, 24000, 16000, sizes), whole, str(sizes))

    def test_what_the_lower_rate_cannot_carry_is_removed_not_folded_back(self):
        out = run(tone(10000, 24000).tobytes(), 24000, 16000)     # 10 kHz would fold to 6 kHz at 16 kHz
        self.assertLess(level_at(out, 6000, 16000) / 12000, .01)
        self.assertLess(np.abs(out[len(out) // 4: 3 * len(out) // 4]).max() / 12000, .02)

    def test_a_steady_level_stays_and_loud_speech_does_not_wrap(self):
        steady = run(np.full(24000, 5000, "<i2").tobytes(), 24000, 16000)
        self.assertTrue(np.all(np.abs(steady[2000:-2000] - 5000) <= 1))
        loud = run(tone(3000, 24000, level=32767).tobytes(), 24000, 16000)
        self.assertLessEqual(np.abs(loud).max(), 32768)       # clipped at full scale, never wrapped
        self.assertGreater(level_at(loud, 3000, 16000), 32000)

    def test_nothing_in_nothing_out(self):
        self.assertEqual(Resampler(24000, 16000)(b"", last=True), b"")
        self.assertEqual(len(run(tone(500, 24000, .001).tobytes(), 24000, 16000)), 16)


if __name__ == "__main__":
    unittest.main()
