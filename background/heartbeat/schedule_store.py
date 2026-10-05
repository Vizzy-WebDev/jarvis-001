"""When each watched item is next due — persisted, so a restart resumes.

The whole reason this is a table rather than a set of in-memory timers: a
restart must not reset every item's clock to zero, and it must not make
everything overdue at once so the first tick after a restart tries to check
everything. `next_due_at` survives; the tick takes what is due, oldest first,
up to a cap.

A leaf: the database and the clock. `id` is deterministic (`source:item`), so
registering an item is a plain upsert rather than a lookup-then-decide.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from ..db import get_db
from ..jscompat import now_iso, to_iso_z


def _row_id(source_id: str, item_key: str) -> str:
    return f"{source_id}:{item_key}"


def _item(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"], "sourceId": row["source_id"], "itemKey": row["item_key"],
        "intervalMs": row["interval_ms"], "nextDueAt": row["next_due_at"],
        "lastCheckedAt": row["last_checked_at"], "running": bool(row["running"]),
        "checkState": json.loads(row["check_state"]) if row["check_state"] else None,
        "createdAt": row["created_at"],
    }


def upsert_item(source_id: str, item_key: str, interval_ms: int) -> dict[str, Any]:
    """Register (or re-register) one item.

    A NEW item is due immediately rather than a full interval from now:
    something a source has only just started watching is worth a first look
    promptly. An item that already exists keeps its real due time even if the
    interval changed — retuning a cadence must not itself cause a burst of
    immediate re-checks.
    """
    db = get_db()
    row_id = _row_id(source_id, item_key)
    existing = db.execute("SELECT * FROM heartbeat_schedule WHERE id = ?", (row_id,)).fetchone()
    if existing is None:
        stamp = now_iso()
        db.execute(
            "INSERT INTO heartbeat_schedule (id, source_id, item_key, interval_ms, "
            "next_due_at, running, created_at) VALUES (?, ?, ?, ?, ?, 0, ?)",
            (row_id, source_id, item_key, interval_ms, stamp, stamp))
    elif existing["interval_ms"] != interval_ms:
        db.execute("UPDATE heartbeat_schedule SET interval_ms = ? WHERE id = ?",
                   (interval_ms, row_id))
    return _item(db.execute("SELECT * FROM heartbeat_schedule WHERE id = ?", (row_id,)).fetchone())


def list_due(now: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    rows = get_db().execute(
        "SELECT * FROM heartbeat_schedule WHERE next_due_at <= ? AND running = 0 "
        "ORDER BY next_due_at ASC LIMIT ?", (now or now_iso(), limit)).fetchall()
    return [_item(r) for r in rows]


def mark_running(row_id: str) -> None:
    get_db().execute("UPDATE heartbeat_schedule SET running = 1 WHERE id = ?", (row_id,))


_UNSET = object()


def mark_done(row_id: str, check_state: Any = _UNSET) -> None:
    """Advance past this check — called in a `finally`, success or failure alike,
    so a check that throws still moves forward instead of retrying every tick."""
    db = get_db()
    row = db.execute("SELECT interval_ms FROM heartbeat_schedule WHERE id = ?",
                     (row_id,)).fetchone()
    if row is None:
        return
    next_due = to_iso_z(datetime.now(timezone.utc) + timedelta(milliseconds=row["interval_ms"]))
    if check_state is _UNSET:
        db.execute("UPDATE heartbeat_schedule SET running = 0, last_checked_at = ?, "
                   "next_due_at = ? WHERE id = ?", (now_iso(), next_due, row_id))
        return
    db.execute("UPDATE heartbeat_schedule SET running = 0, last_checked_at = ?, "
               "next_due_at = ?, check_state = ? WHERE id = ?",
               (now_iso(), next_due, None if check_state is None else json.dumps(check_state),
                row_id))


def reset_stale_running() -> int:
    """Startup only. A `running=1` row left by a crash mid-check would otherwise
    wedge that item forever — this is the one place it is ever cleared without a
    matching `mark_done`, and calling it mid-process would let two checks of the
    same item overlap."""
    cursor = get_db().execute("UPDATE heartbeat_schedule SET running = 0 WHERE running = 1")
    return int(cursor.rowcount or 0)


def prune_removed(source_id: str, valid_item_keys: list[str]) -> int:
    valid = set(valid_item_keys)
    rows = get_db().execute(
        "SELECT item_key FROM heartbeat_schedule WHERE source_id = ?", (source_id,)).fetchall()
    gone = [r["item_key"] for r in rows if r["item_key"] not in valid]
    for item_key in gone:
        get_db().execute("DELETE FROM heartbeat_schedule WHERE id = ?",
                         (_row_id(source_id, item_key),))
    return len(gone)


def get_item(source_id: str, item_key: str) -> dict[str, Any] | None:
    row = get_db().execute("SELECT * FROM heartbeat_schedule WHERE id = ?",
                           (_row_id(source_id, item_key),)).fetchone()
    return _item(row) if row is not None else None


def list_for_source(source_id: str) -> list[dict[str, Any]]:
    rows = get_db().execute(
        "SELECT * FROM heartbeat_schedule WHERE source_id = ? ORDER BY item_key",
        (source_id,)).fetchall()
    return [_item(r) for r in rows]
