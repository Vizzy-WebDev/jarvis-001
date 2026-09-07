"""Is there actually a person to reach right now?

Two signals, deliberately separate, because they mean different things and an
emergency should skip only one of them:

* `is_reachable()` — a UI is connected AND the user has been active recently.
  The hard requirement: without it there is no plausible way to deliver
  anything live, emergency or not.
* `is_busy()` — they appear to be in a call. A dampener, not a gate, and
  skippable for something genuinely urgent.

Being unreachable never drops a finding. It only means the finding waits for the
next time the user starts a conversation, which is what the outbox is for.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

#: How recently the user must actually have said something for "here" to mean
#: anything. A tab left open overnight is not a person in the room.
IDLE_WINDOW = timedelta(minutes=30)

#: A starting list, and an admittedly incomplete one: a call in a browser tab is
#: not detectable this way at all. Advisory only — a miss here means a message
#: was not held back when perhaps it should have been, never a lost finding.
BUSY_PROCESS_NAMES = ("zoom", "teams", "discord", "skype", "webex", "slack call", "facetime")


def _connected_clients() -> int:
    """How many UI consumers are attached to the event stream."""
    from ..events import bus

    with bus._lock:                       # noqa: SLF001 — the count is the bus's own state
        return len(bus._queues)           # noqa: SLF001


def is_reachable(now: datetime | None = None) -> bool:
    from ..chat_store import get_active_id, get_last_user_message_at

    if _connected_clients() == 0:
        return False
    conversation_id = get_active_id()
    if not conversation_id:
        return False
    last = get_last_user_message_at(conversation_id)
    if not last:
        return False
    moment = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
    return (now or datetime.now(timezone.utc)) - moment <= IDLE_WINDOW


def is_busy() -> bool:
    """Best effort. Not being able to tell reads as "not busy" — this must never
    be the thing standing between a finding and the person it concerns."""
    try:
        import psutil

        for process in psutil.process_iter(["name"]):
            name = str(process.info.get("name") or "").lower()
            if any(busy in name for busy in BUSY_PROCESS_NAMES):
                return True
    except Exception:  # noqa: BLE001 — a restricted environment refuses this outright
        logger.debug("could not read the process list", exc_info=True)
    return False


def is_available(now: datetime | None = None) -> bool:
    """The ordinary-flow check: reachable AND not apparently busy. An emergency
    path calls `is_reachable()` alone — see this module's docstring."""
    return is_reachable(now) and not is_busy()
