"""Whether Jarvis is looking at the screen right now, and whether it may.

Two different things, deliberately kept apart:

* **An observation** is one capture, for one question. `observe()` is a context
  manager: the badge is lit for the whole span — from just before the capture
  until the answer is finished, not just the instant the pixels are grabbed —
  because the point is to reflect real observation, not the OS call. Several can
  overlap (a glance while a monitor is watching), so tokens are counted, not a
  boolean.
* **Screen sharing** is a MODE the user turns on and leaves on. It is not a
  glance that happens to be long. It arms the badge and tells the model, through
  a prompt section, that the next screen question needs no "look at my screen"
  first — and turning it on never itself describes anything, since nothing was
  asked yet.

A spoken instruction and a UI toggle both land here, so the two can never
disagree about what is happening.

State lives in this process, not on disk: "am I looking at your screen right
now" must be false after a restart, and a file that said otherwise would be
lying about the current moment.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass

from ..events import EventType, bus as default_bus

_lock = threading.RLock()
_tokens: dict[str, str] = {}          # token -> reason
_sharing = False


@dataclass(frozen=True)
class WatchState:
    watching: bool
    sharing: bool
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {"watching": self.watching, "sharing": self.sharing,
                "reasons": list(self.reasons)}


def state() -> WatchState:
    with _lock:
        return WatchState(watching=bool(_tokens) or _sharing, sharing=_sharing,
                          reasons=tuple(_tokens.values()))


def _announce(bus=None) -> None:
    (bus or default_bus).publish(EventType.SCREEN_WATCH, state().as_dict())


def show_indicator(reason: str, *, bus=None) -> str:
    """Light the badge and get a token back. Always release it."""
    token = secrets.token_hex(6)
    with _lock:
        _tokens[token] = reason
    _announce(bus)
    return token


def hide_indicator(token: str, *, bus=None) -> None:
    with _lock:
        existed = _tokens.pop(token, None) is not None
    if existed:
        _announce(bus)


class observe:
    """`with observe("Jarvis is looking at your screen"):` — released on every
    path out, including a raised error."""

    def __init__(self, reason: str, *, bus=None) -> None:
        self._reason = reason
        self._bus = bus
        self._token: str | None = None

    def __enter__(self) -> "observe":
        self._token = show_indicator(self._reason, bus=self._bus)
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._token:
            hide_indicator(self._token, bus=self._bus)
            self._token = None


def is_sharing() -> bool:
    with _lock:
        return _sharing


def start_sharing(*, bus=None) -> WatchState:
    global _sharing
    with _lock:
        _sharing = True
    _announce(bus)
    return state()


def stop_sharing(*, bus=None) -> WatchState:
    global _sharing
    with _lock:
        _sharing = False
    _announce(bus)
    return state()


def stop_everything(*, bus=None) -> WatchState:
    """One "stop whatever is making this badge lit" action — what clicking the
    badge itself does."""
    global _sharing
    with _lock:
        _tokens.clear()
        _sharing = False
    _announce(bus)
    return state()


def reset_for_tests() -> None:
    global _sharing
    with _lock:
        _tokens.clear()
        _sharing = False
