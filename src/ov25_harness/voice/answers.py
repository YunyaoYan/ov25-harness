# SPDX-License-Identifier: Apache-2.0
# From the earlier OVStreaming voice implementation. See NOTICE for attribution.
# The module body is unchanged.
"""Which OV answers to speak, and how much of each to remember.

This module knows nothing about the voice framework around it. The caller
reports what happened (an answer finished, the user started talking, audio was
played) and carries out the returned actions.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Speak:
    turn: int
    text: str


@dataclass(frozen=True)
class Interrupt:
    turn: int


@dataclass(frozen=True)
class Commit:
    response_id: str
    text: str


Action = Speak | Interrupt | Commit


@dataclass
class _Reply:
    turn: int
    response_id: str
    text: str
    # (characters of text, audio ms) at each spoken sentence end.
    progress: list[tuple[int, float]] = field(default_factory=list)
    delivered_ms: float | None = None
    heard_ms: float | None = None


class SpokenAnswers:
    """Speaks finished answers one at a time, never over the user.

    An answer is committed as memory once the user has received it: all of it
    when its audio finishes, or the sentences played before the user cut in.
    The cut is resolved by flush(), which the caller runs before sending the
    next input, so it lands in the turn that would have received it anyway.
    """

    def __init__(self) -> None:
        self._waiting: deque[tuple[str, str]] = deque()
        self._speaking: _Reply | None = None
        self._cut: _Reply | None = None
        self._user_speaking = False
        self._turns = 0

    def answer(self, response_id: str, text: str) -> list[Action]:
        self._waiting.append((response_id, text))
        return self._next()

    def spoken(self, turn: int) -> list[Action]:
        reply = self._speaking
        if reply is None or reply.turn != turn:
            return []
        self._speaking = None
        return [Commit(reply.response_id, reply.text), *self._next()]

    def progress(self, turn: int, text_end: int, audio_ms: float) -> None:
        for reply in (self._speaking, self._cut):
            if reply is not None and reply.turn == turn:
                reply.progress.append((text_end, audio_ms))

    def user_started(self) -> list[Action]:
        """Stop the answer being spoken and drop the ones not yet spoken."""
        return [*self.user_spoke(), *self.user_paused()]

    def user_paused(self) -> list[Action]:
        """The microphone opened: stop speaking, but keep what is unspoken.

        Whether this really was the user is not known until the transcript
        comes back. An empty or spurious one has to leave the queued answers
        alone, so dropping them belongs in user_spoke(), not here.
        """
        self._user_speaking = True
        reply, self._speaking = self._speaking, None
        if reply is None:
            return []
        self._cut = reply
        return [*self.flush(force=True), Interrupt(reply.turn)]

    def user_spoke(self) -> list[Action]:
        """A real question arrived, so answers still queued are stale."""
        actions: list[Action] = [Commit(rid, "") for rid, _ in self._waiting]
        self._waiting.clear()
        return actions

    def user_stopped(self) -> list[Action]:
        self._user_speaking = False
        return self._next()

    def delivered(self, turn: int, audio_ms: float) -> None:
        if self._cut is not None and self._cut.turn == turn:
            self._cut.delivered_ms = audio_ms

    def heard(self, audio_ms: float) -> None:
        if self._cut is not None:
            self._cut.heard_ms = audio_ms

    def flush(self, *, force: bool = False) -> list[Action]:
        """Commit the interrupted answer up to the last sentence the user heard.

        Without a playback report the answer waits, unless forced, which
        commits nothing of it.
        """
        reply = self._cut
        if reply is None:
            return []
        known = [ms for ms in (reply.delivered_ms, reply.heard_ms) if ms is not None]
        if not known and not force:
            return []
        self._cut = None
        heard_ms = min(known, default=0.0)
        end = max((end for end, ms in reply.progress if ms <= heard_ms), default=0)
        return [Commit(reply.response_id, reply.text[:end])]

    def _next(self) -> list[Action]:
        if self._speaking is not None or self._user_speaking or not self._waiting:
            return []
        response_id, text = self._waiting.popleft()
        self._turns += 1
        self._speaking = _Reply(self._turns, response_id, text)
        return [Speak(self._turns, text)]
