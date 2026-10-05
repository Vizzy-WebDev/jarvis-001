"""Staying in the conversation after the wake word (§16).

"After responding, allow a short follow-up window so the user can continue
speaking without repeating the wake word. Close the session after a period of
silence."

Pure timing and state — no audio, no I/O, no model — so the whole thing can be
checked as a truth table over a fake clock. Every real decision this makes is
about WHEN, and testing "when" against a real clock is how a test becomes slow
and flaky at the same time.

Two properties worth stating:

* **The window is measured from the last thing that actually happened**, not from
  the wake. A back-and-forth that keeps going never times out mid-conversation,
  and a wake nobody followed up on closes on schedule.

* **The assistant speaking counts as activity, and so does a tool running.**
  Otherwise a long answer or a slow lookup closes the window while the user is
  still listening to it, and their reply arrives at a closed session.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum

#: How long the session stays open with nothing happening. Long enough to think
#: about what to say next, short enough that a room does not stay live.
DEFAULT_IDLE_TIMEOUT_S = 25.0

#: A hard ceiling regardless of activity, so a session cannot be kept open
#: indefinitely by background chatter alone.
DEFAULT_MAX_SESSION_S = 10 * 60.0


class Mode(str, Enum):
    #: Not listening for speech; only the wake word can start anything.
    ASLEEP = "asleep"
    #: Awake and taking follow-ups without the wake word.
    OPEN = "open"


@dataclass(frozen=True)
class Transition:
    mode: Mode
    reason: str
    at: float


class ConversationMode:
    def __init__(self, *, idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S,
                 max_session_s: float = DEFAULT_MAX_SESSION_S) -> None:
        self.idle_timeout_s = idle_timeout_s
        self.max_session_s = max_session_s
        self._mode = Mode.ASLEEP
        self._opened_at = 0.0
        self._last_activity = 0.0
        self._lock = threading.RLock()

    @property
    def mode(self) -> Mode:
        return self._mode

    @property
    def is_open(self) -> bool:
        return self._mode is Mode.OPEN

    def wake(self, now: float | None = None, reason: str = "wake word") -> Transition:
        """The wake word fired, or the user pressed the button."""
        now = time.time() if now is None else now
        with self._lock:
            self._mode = Mode.OPEN
            self._opened_at = now
            self._last_activity = now
        return Transition(Mode.OPEN, reason, now)

    def touch(self, now: float | None = None) -> None:
        """Something happened: speech, a reply, a tool running. Resets the idle
        clock without extending the hard ceiling."""
        now = time.time() if now is None else now
        with self._lock:
            if self._mode is Mode.OPEN:
                self._last_activity = now

    def close(self, now: float | None = None, reason: str = "closed") -> Transition:
        now = time.time() if now is None else now
        with self._lock:
            self._mode = Mode.ASLEEP
        return Transition(Mode.ASLEEP, reason, now)

    def tick(self, now: float | None = None) -> Transition | None:
        """Close the session if it has run out of time. Returns the transition,
        or None if nothing changed — a caller can run this as often as it likes."""
        now = time.time() if now is None else now
        with self._lock:
            if self._mode is not Mode.OPEN:
                return None
            if now - self._opened_at >= self.max_session_s:
                return self.close(now, "open too long")
            if now - self._last_activity >= self.idle_timeout_s:
                return self.close(now, "no one said anything")
        return None

    def seconds_left(self, now: float | None = None) -> float:
        """How long until this closes, if nothing else happens. 0 when asleep."""
        now = time.time() if now is None else now
        with self._lock:
            if self._mode is not Mode.OPEN:
                return 0.0
            return max(0.0, min(self.idle_timeout_s - (now - self._last_activity),
                                self.max_session_s - (now - self._opened_at)))
