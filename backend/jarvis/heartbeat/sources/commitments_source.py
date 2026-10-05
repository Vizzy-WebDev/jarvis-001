"""Things the user is committed to that are about to matter.

Watches memories carrying a real expiry — the field extraction fills in for
anything that stops being true on a known date ("in Lisbon until the 14th",
"the deposit is due on the 3rd"). Two moments are worth noticing: shortly
before, while there is still time to act, and once it has passed, because a
memory that has quietly lapsed is one Jarvis will otherwise stop mentioning
entirely.

**Deliberately built on the structured field rather than on parsing dates out of
prose.** A date extracted from free text by regex is wrong often enough that the
notifications it produces would be wrong often enough to ignore, and this is a
channel that only works while it is trusted. A commitment becomes time-sensitive
here when something actually recorded a date for it.

Its own dedup memory, per the source contract: "due tomorrow" stays true for a
whole day, and one notification for it is the point.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .registry import Source

SOURCE_ID = "commitments"
CHECK_INTERVAL_MS = 30 * 60 * 1000
#: How long before it lands is worth mentioning.
APPROACHING = timedelta(hours=24)


def _when(memory: dict[str, Any]) -> datetime | None:
    raw = memory.get("expiresAt")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _commitments() -> list[dict[str, Any]]:
    from ...memory import store as memory_store

    # Expired ones included on purpose: a lapsed commitment is exactly what this
    # exists to mention, and the default read hides it.
    return [m for m in memory_store.list_memories(include_expired=True) if _when(m)]


def list_items() -> list[dict[str, Any]]:
    return [{"itemKey": m["id"], "intervalMs": CHECK_INTERVAL_MS} for m in _commitments()]


def check(item_key: str, now: datetime | None = None) -> dict[str, Any]:
    from ...heartbeat import schedule_store
    from ...memory import store as memory_store

    memory = memory_store.get_memory(item_key)
    when = _when(memory) if memory else None
    if memory is None or when is None or memory.get("archived"):
        return {"finding": None, "checkState": None}

    now = now or datetime.now(timezone.utc)
    item = schedule_store.get_item(SOURCE_ID, item_key)
    state = dict(((item or {}).get("checkState") or {}))

    if now >= when:
        if state.get("lapsed"):
            return {"finding": None}
        state["lapsed"] = True
        state["approaching"] = True      # never mention it as upcoming afterwards
        return {"finding": {"summary": f"Something you told me has now passed: {memory['text']}",
                            "detail": {"memoryId": item_key, "at": memory["expiresAt"]}},
                "checkState": state}

    if when - now <= APPROACHING:
        if state.get("approaching"):
            return {"finding": None}
        state["approaching"] = True
        hours = max(1, int((when - now).total_seconds() // 3600))
        return {"finding": {"summary": f"Coming up in about {hours} hour"
                                       f"{'s' if hours != 1 else ''}: {memory['text']}",
                            "detail": {"memoryId": item_key, "at": memory["expiresAt"]}},
                "checkState": state}

    return {"finding": None}


source = Source(id=SOURCE_ID, default_interval_ms=CHECK_INTERVAL_MS,
                list_items=list_items, check=check)
