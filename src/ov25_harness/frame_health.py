"""Whether a live session is getting the frames the streaming model was trained on: one a second.

Fewer than MIN_FRAMES accepted frames in the last WINDOW_S seconds is a low frame rate. Frames that
arrive later and later than their own timestamps say are a slow network: the delay of a frame is how
much longer it took than the fastest frame of the session. The session is told once each time either
gets worse; a live session without a frame for [policy] live_idle_s is closed by the app.
"""
from collections import deque
import math
import time

WINDOW_S = 10.
MIN_FRAMES = 8
MAX_DELAY_S = 2.
OK, LOW, DELAYED = "ok", "low_frame_rate", "network_delay"


def _english(key, **fields):
    from .locale import Locale

    return Locale(("en",)).message("en", key, **fields)


class FrameHealth:
    def __init__(self, now=None, *, say=None):
        self.opened = time.monotonic() if now is None else now
        # The warning's words: say(key, **fields) -> str, from the session's locale.
        self.say = say or _english
        self.arrivals = deque(maxlen=64)
        self.last_t = 0.
        # Arrival time minus declared time of the frame that came fastest.
        self.offset = None
        self.delay_s = 0.
        self.warned = OK
        self.warned_at = -math.inf

    def frame(self, t, now):
        """An accepted frame declared at session second t arrived at monotonic time now."""
        self.arrivals.append(now)
        self.last_t = t
        self.offset = now - t if self.offset is None else min(self.offset, now - t)
        self.delay_s = now - t - self.offset

    def idle_s(self, now):
        return now - (self.arrivals[-1] if self.arrivals else self.opened)

    def frames(self, now):
        return sum(now - arrived <= WINDOW_S for arrived in self.arrivals)

    def status(self, now):
        if not self.arrivals:
            return OK
        if self.delay_s > MAX_DELAY_S:
            return DELAYED
        # A session younger than the window cannot have filled it yet.
        if now - self.arrivals[0] >= WINDOW_S and self.frames(now) < MIN_FRAMES:
            return LOW
        return OK

    def summary(self, now):
        return dict(status=self.status(now), frames_10s=self.frames(now), delay_s=round(self.delay_s, 2))

    def warning(self, now):
        """The row to send the session when its frames just got worse, else None."""
        status = self.status(now)
        if status == OK:
            self.warned = OK
            return None
        if status == self.warned or now - self.warned_at < WINDOW_S:
            return None
        self.warned, self.warned_at = status, now
        if status == DELAYED:
            text = self.say("network_delay", delay=f"{self.delay_s:.0f}")
        else:
            text = self.say("low_frame_rate", window=f"{WINDOW_S:.0f}", frames=self.frames(now))
        return dict(type="warning", t=self.last_t, text=text, **self.summary(now))
