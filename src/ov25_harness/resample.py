"""Speech at the rate a client asked for.

The synthesizer speaks at one rate (24 kHz) and a client may ask for another. Each output sample
is a windowed-sinc sum over the input around it, cut off at what the lower of the two rates can
carry, so going down does not alias. The filter runs across chunk edges: a sentence comes out the
same however the synthesizer's stream happened to be cut.
"""
import math

import numpy as np

# Zero crossings of the sinc kept on each side of an output sample.
ZEROS = 16


class Resampler:
    """s16le mono audio from one rate to another, a chunk at a time. Call with last=True to end."""

    def __init__(self, src_rate, dst_rate):
        self.step = src_rate / dst_rate                 # input samples per output sample
        self.cut = min(1., dst_rate / src_rate)         # pass band, as a share of the input's
        self.reach = math.ceil(ZEROS / self.cut)        # input samples the filter needs on each side
        self.taps = np.arange(-self.reach + 1, self.reach + 1)
        self.held = np.zeros(0, np.float32)             # input the filter still needs
        self.first = 0                                  # where held[0] is in the stream
        self.made = 0                                   # output samples produced
        self.spare = b""                                # half a sample left over from a chunk

    def __call__(self, pcm, last=False):
        data = self.spare + pcm
        whole = len(data) & ~1
        self.spare = data[whole:]
        self.held = np.concatenate([self.held, np.frombuffer(data[:whole], "<i2").astype(np.float32)])
        seen = self.first + len(self.held)
        # An output sample waits for the input after it, unless there will be none.
        ready = seen if last else seen - self.reach
        count = max(0, math.ceil(ready / self.step) - self.made)
        if not count:
            return b""
        centre = (self.made + np.arange(count)) * self.step
        index = np.floor(centre).astype(np.int64)[:, None] + self.taps
        distance = centre[:, None] - index
        weight = np.sinc(self.cut * distance) * (.5 + .5 * np.cos(np.pi * distance / self.reach))
        weight /= weight.sum(axis=1, keepdims=True)
        # Before the first sample and after the last there is silence.
        inside = (index >= self.first) & (index < seen)
        samples = np.where(inside, self.held[np.clip(index - self.first, 0, len(self.held) - 1)], 0.)
        out = (samples * weight).sum(axis=1)
        self.made += count
        keep = max(self.first, math.floor(self.made * self.step) - self.reach + 1)
        self.held, self.first = self.held[keep - self.first:], keep
        return np.clip(np.rint(out), -32768, 32767).astype("<i2").tobytes()
