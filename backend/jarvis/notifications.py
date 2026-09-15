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

import logging
import os
import random
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from .jscompat import now_iso
from .store import read_json, write_json

logger = logging.getLogger(__name__)

FILE = "notifications"
MAX_KEPT = 200
#: Long enough to cover a fault that keeps happening, short enough that "this
#: again, later" gets its own row.
DEDUP_WINDOW_S = 10 * 60

#: How long something sits in the recycle bin before it is gone for good.
TRASH_RETENTION_DAYS = 30
#: Twice a day is plenty of resolution for a 30-day window.
_PURGE_INTERVAL_S = 6 * 60 * 60
#: Named like every other background-clock subsystem's own flag (see
#: `jarvis/ops/environment/sampler.py`) so `start_background_work()`'s test
#: coverage can read it the same way: `notifications.ENABLE_ENV`.
ENABLE_ENV = "JARVIS_NOTIFICATION_TRASH_PURGE"

_purge_timer: threading.Timer | None = None


def _load() -> dict[str, Any]:
    data = read_json(FILE, {"notifications": []})
    if not isinstance(data, dict) or not isinstance(data.get("notifications"), list):
        return {"notifications": []}
    return data


def _new_id() -> str:
    stamp = f"{int(time.time() * 1000):x}"
    tail = "".join(random.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(6))
    return f"n{stamp}{tail}"


def _parse_iso(iso: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_seconds(iso: str) -> float:
    when = _parse_iso(iso)
    if when is None:
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
        # No "trashedAt" key at all until something actually trashes it — see
        # `remove()`. GET /api/notifications is contract-tested against a
        # recording with no such field, and every row it can ever return is
        # by definition one that was never trashed (the active listing
        # excludes anything with the key), so this keeps that response
        # byte-identical rather than adding a field the recording never had.
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


def listed(limit: int | None = None, *, include_trashed: bool = False) -> list[dict[str, Any]]:
    rows = _load()["notifications"]
    if not include_trashed:
        rows = [row for row in rows if not row.get("trashedAt")]
    return rows[:limit] if isinstance(limit, int) else rows


def trash_listed(limit: int | None = None) -> list[dict[str, Any]]:
    """What is sitting in the recycle bin, most recently trashed first."""
    rows = [row for row in _load()["notifications"] if row.get("trashedAt")]
    rows.sort(key=lambda row: row.get("trashedAt") or "", reverse=True)
    return rows[:limit] if isinstance(limit, int) else rows


def unread_count() -> int:
    return sum(1 for row in _load()["notifications"]
              if not row.get("read") and not row.get("trashedAt"))


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
    """Move one to the recycle bin. Not a hard delete — see `purge()` for that."""
    data = _load()
    for row in data["notifications"]:
        if row.get("id") == notification_id and not row.get("trashedAt"):
            row["trashedAt"] = now_iso()
            write_json(FILE, data)
            return True
    return any(row.get("id") == notification_id for row in data["notifications"])


def clear_all() -> None:
    """Move every active notification to the recycle bin.

    Never touches a row already trashed — overwriting its `trashedAt` would
    reset the 30-day clock on something a previous Clear already put there.
    """
    data = _load()
    changed = False
    stamp = now_iso()
    for row in data["notifications"]:
        if not row.get("trashedAt"):
            row["trashedAt"] = stamp
            changed = True
    if changed:
        write_json(FILE, data)


def restore(notification_id: str) -> bool:
    """Bring one back out of the recycle bin.

    Removes the "trashedAt" key entirely rather than setting it to None, so a
    restored row is indistinguishable from one that was never trashed — see
    the note in `add()` on why the active listing must never carry the key.
    """
    data = _load()
    for row in data["notifications"]:
        if row.get("id") == notification_id and row.get("trashedAt"):
            row.pop("trashedAt", None)
            write_json(FILE, data)
            return True
    return False


def purge(notification_id: str) -> bool:
    """Permanently delete one, whether or not it was ever trashed first."""
    data = _load()
    remaining = [r for r in data["notifications"] if r.get("id") != notification_id]
    if len(remaining) == len(data["notifications"]):
        return False
    data["notifications"] = remaining
    write_json(FILE, data)
    return True


def empty_trash() -> int:
    """Permanently delete everything currently in the recycle bin. Returns how many."""
    data = _load()
    keep = [r for r in data["notifications"] if not r.get("trashedAt")]
    removed = len(data["notifications"]) - len(keep)
    if removed:
        data["notifications"] = keep
        write_json(FILE, data)
    return removed


def purge_expired_trash(*, older_than_days: int = TRASH_RETENTION_DAYS) -> int:
    """Permanently delete anything that has sat in the bin past its time."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    data = _load()
    keep = []
    removed = 0
    for row in data["notifications"]:
        trashed_at = row.get("trashedAt")
        parsed = _parse_iso(trashed_at) if trashed_at else None
        if parsed is not None and parsed < cutoff:
            removed += 1
            continue
        keep.append(row)
    if removed:
        data["notifications"] = keep
        write_json(FILE, data)
    return removed


# --- the recycle bin's own clock -----------------------------------------------

def is_enabled() -> bool:
    return os.environ.get(ENABLE_ENV) == "1"


def start_trash_purge() -> bool:
    global _purge_timer
    if not is_enabled() or _purge_timer is not None:
        return False

    def run() -> None:
        global _purge_timer
        try:
            removed = purge_expired_trash()
            if removed:
                logger.info("[notifications] purged %d expired recycle-bin item(s)", removed)
        except Exception:  # noqa: BLE001 — housekeeping must not break the process
            logger.exception("purging the notifications recycle bin failed")
        _purge_timer = threading.Timer(_PURGE_INTERVAL_S, run)
        _purge_timer.daemon = True
        _purge_timer.start()

    run()
    return True


def stop_trash_purge() -> None:
    global _purge_timer
    if _purge_timer is not None:
        _purge_timer.cancel()
        _purge_timer = None


def reset_for_tests() -> None:
    stop_trash_purge()
