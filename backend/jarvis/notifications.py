"""Everything Jarvis needs to tell the user, kept until they have seen it.

Subsystems already publish `NOTIFICATION_CREATED` on the bus and nothing was
listening, so a scheduled task that failed at 3am announced itself to an empty
room. This is the room: a small persistent store, plus an observer that writes
what the bus publishes.

**A repeating fault collapses into one row with a count.** Found in the real
store this replaces: a front-end watchdog firing on nearly every turn while the
whole model roster was rate-limited pushed 102 identical rows into a 200-row
cap, evicting the findings worth reading. The match is deliberately exact —
same kind AND title, still unread, and recent — and only ever against the newest
row, never a scan back through history, so two unrelated notices that happen to
share wording are never merged.

`kind` is a free string (`task_run`, `monitor`, `connector`, `heartbeat`, …),
not an enum. A new kind of notice needs no change here and none in the client.
"""

from __future__ import annotations

import random
import time
from typing import Any

from .jscompat import now_iso
from .store import read_json, write_json

FILE = "notifications"
MAX_KEPT = 200
#: Long enough to cover a fault that keeps happening, short enough that "this
#: again, later" gets its own row.
DEDUP_WINDOW_S = 10 * 60


def _load() -> dict[str, Any]:
    data = read_json(FILE, {"notifications": []})
    if not isinstance(data, dict) or not isinstance(data.get("notifications"), list):
        return {"notifications": []}
    return data


def _new_id() -> str:
    stamp = f"{int(time.time() * 1000):x}"
    tail = "".join(random.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(6))
    return f"n{stamp}{tail}"


def _age_seconds(iso: str) -> float:
    from datetime import datetime, timezone

    try:
        when = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return DEDUP_WINDOW_S + 1
    return (datetime.now(timezone.utc) - when).total_seconds()


def add(*, kind: str = "system", level: str = "info", title: str = "", body: str = "",
        action: dict[str, Any] | None = None, meta: dict[str, Any] | None = None,
        event_bus: Any = None) -> dict[str, Any]:
    """Record one, and push it to any open tab."""
    if not title:
        raise ValueError("A notification needs a title.")
    data = _load()
    rows = data["notifications"]

    newest = rows[0] if rows else None
    if (newest and not newest.get("read") and newest.get("kind") == kind
            and newest.get("title") == title
            and 0 <= _age_seconds(newest.get("ts", "")) <= DEDUP_WINDOW_S):
        newest["count"] = int(newest.get("count") or 1) + 1
        newest["ts"] = now_iso()
        # A repeat's detail can differ even when the title does not; the
        # freshest reason is the useful one.
        newest["body"] = body or newest.get("body") or ""
        newest["action"] = action or newest.get("action")
        newest["meta"] = meta or newest.get("meta")
        write_json(FILE, data)
        _announce(newest, event_bus)
        return newest

    notification = {
        "id": _new_id(), "kind": kind, "level": level, "title": title,
        "body": body or "", "action": action or None, "meta": meta or None,
        "ts": now_iso(), "read": False, "count": 1,
    }
    rows.insert(0, notification)
    data["notifications"] = rows[:MAX_KEPT]
    write_json(FILE, data)
    _announce(notification, event_bus)
    return notification


def _announce(notification: dict[str, Any], event_bus: Any = None) -> None:
    """Tell open tabs. Never the bus event that got us here — that would be a
    loop: the observer writes what the bus publishes."""
    from .events import EventType
    from .events import bus as default_bus

    (event_bus or default_bus).publish(EventType.NOTIFICATION_STORED,
                                       {"notification": notification})


def listed(limit: int | None = None) -> list[dict[str, Any]]:
    rows = _load()["notifications"]
    return rows[:limit] if isinstance(limit, int) else rows


def unread_count() -> int:
    return sum(1 for row in _load()["notifications"] if not row.get("read"))


def mark_read(ids: Any) -> list[dict[str, Any]]:
    wanted = {str(i) for i in (ids if isinstance(ids, (list, tuple, set)) else [ids])}
    data = _load()
    changed = False
    for row in data["notifications"]:
        if row.get("id") in wanted and not row.get("read"):
            row["read"] = True
            changed = True
    if changed:
        write_json(FILE, data)
    return data["notifications"]


def mark_all_read() -> list[dict[str, Any]]:
    data = _load()
    changed = False
    for row in data["notifications"]:
        if not row.get("read"):
            row["read"] = True
            changed = True
    if changed:
        write_json(FILE, data)
    return data["notifications"]


def remove(notification_id: str) -> bool:
    data = _load()
    remaining = [r for r in data["notifications"] if r.get("id") != notification_id]
    if len(remaining) == len(data["notifications"]):
        return False
    data["notifications"] = remaining
    write_json(FILE, data)
    return True


def clear_all() -> None:
    write_json(FILE, {"notifications": []})
