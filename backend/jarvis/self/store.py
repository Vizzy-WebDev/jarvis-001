"""What Jarvis has actually observed about itself.

Two small tables of its own — a reliability tally per capability, and declared
goals — plus the health of the recorder itself, plus the snapshots that make a
self-claim checkable after the fact.

**The recorder's own health is recorded.** Without it, "this capability has never
been used" and "the thing that records usage is broken" are indistinguishable:
both read as an empty tally. That was found by an audit rather than designed in,
and it is the difference between silence meaning something and meaning nothing.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from ..db import get_db
from ..jscompat import now_iso

logger = logging.getLogger(__name__)


# --- the reliability tally ---------------------------------------------------

def record_attempt(axis: str, key: str, ok: bool) -> bool:
    """One more use of something, and whether it worked. Returns whether the
    write itself succeeded — the caller records that too, so a broken recorder
    is visible rather than silent."""
    try:
        stamp = now_iso()
        get_db().execute(
            "INSERT INTO self_capability_stats (axis, key, attempts, failures, last_ok_at, "
            "last_failed_at, updated_at) VALUES (?, ?, 1, ?, ?, ?, ?) "
            "ON CONFLICT(axis, key) DO UPDATE SET "
            "attempts = attempts + 1, failures = failures + ?, "
            "last_ok_at = CASE WHEN ? THEN ? ELSE last_ok_at END, "
            "last_failed_at = CASE WHEN ? THEN last_failed_at ELSE ? END, updated_at = ?",
            (axis, key, 0 if ok else 1, stamp if ok else None, None if ok else stamp, stamp,
             0 if ok else 1, ok, stamp, ok, stamp, stamp))
        record_capture_health(source="self", name=f"{axis}:{key}", ok=True)
        return True
    except Exception as err:  # noqa: BLE001 — the recorder failing must be visible
        logger.exception("could not record an attempt for %s:%s", axis, key)
        record_capture_health(source="self", name=f"{axis}:{key}", ok=False,
                              error_message=str(err))
        return False


def get_stat(axis: str, key: str) -> dict[str, Any] | None:
    row = get_db().execute(
        "SELECT * FROM self_capability_stats WHERE axis = ? AND key = ?", (axis, key)).fetchone()
    return dict(row) if row else None


def list_stats(axis: str | None = None) -> list[dict[str, Any]]:
    if axis:
        rows = get_db().execute(
            "SELECT * FROM self_capability_stats WHERE axis = ? ORDER BY attempts DESC",
            (axis,)).fetchall()
    else:
        rows = get_db().execute(
            "SELECT * FROM self_capability_stats ORDER BY attempts DESC").fetchall()
    return [dict(r) for r in rows]


# --- the recorder's own health ----------------------------------------------

def record_capture_health(*, source: str, ok: bool, name: str | None = None,
                          error_message: str | None = None) -> None:
    try:
        get_db().execute(
            "INSERT INTO capture_health (ts, source, name, ok, error_message) "
            "VALUES (?, ?, ?, ?, ?)",
            (now_iso(), source, name, 1 if ok else 0, error_message))
    except Exception:  # noqa: BLE001 — never let the health log break the thing it watches
        logger.warning("could not record capture health for %s", source)


def capture_health_summary(hours: int = 24) -> dict[str, Any]:
    """How the recorder itself has been doing. This is what lets a
    `no_track_record` answer be told apart from a broken recorder."""
    row = get_db().execute(
        "SELECT COUNT(*) AS attempts, SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failures "
        "FROM capture_health WHERE ts >= datetime('now', ?)", (f"-{int(hours)} hours",)
    ).fetchone()
    attempts = int(row["attempts"] or 0)
    failures = int(row["failures"] or 0)
    return {"attempts": attempts, "failures": failures,
            "healthy": failures == 0,
            "note": ("The recorder is working." if failures == 0 else
                     f"The recorder itself failed {failures} time(s) — an empty track "
                     "record may not mean the capability was never used.")}


# --- snapshots and citations -------------------------------------------------

def save_snapshot(*, conversation_id: str, snapshot: Any, turn_id: str | None = None,
                  tool_call_id: str | None = None) -> str:
    """The exact answer a self-check returned, kept so a claim built on it can be
    checked afterwards against what was actually available."""
    snapshot_id = f"snap_{uuid.uuid4().hex[:12]}"
    get_db().execute(
        "INSERT INTO self_model_snapshots (id, conversation_id, turn_id, tool_call_id, "
        "snapshot_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (snapshot_id, conversation_id, turn_id, tool_call_id,
         json.dumps(snapshot, default=str), now_iso()))
    for name, value in _numeric_fields(snapshot):
        record_citation(snapshot_id=snapshot_id, tool_call_id=tool_call_id,
                        field_name=name, field_value=str(value))
    return snapshot_id


def _numeric_fields(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """Every NUMBER in the snapshot, flattened. Numbers only, deliberately: they
    are the part a later claim can be checked against mechanically."""
    found: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, inner in value.items():
            found += _numeric_fields(inner, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(value, list):
        for index, inner in enumerate(value):
            found += _numeric_fields(inner, f"{prefix}[{index}]")
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        found.append((prefix, value))
    return found


def get_snapshot(snapshot_id: str) -> dict[str, Any] | None:
    row = get_db().execute("SELECT * FROM self_model_snapshots WHERE id = ?",
                           (snapshot_id,)).fetchone()
    if row is None:
        return None
    snapshot = dict(row)
    snapshot["snapshot"] = json.loads(snapshot["snapshot_json"])
    return snapshot


def record_citation(*, snapshot_id: str, field_name: str, field_value: str,
                    tool_call_id: str | None = None) -> None:
    get_db().execute(
        "INSERT INTO self_model_citations (id, snapshot_id, tool_call_id, field_name, "
        "field_value, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (f"cite_{uuid.uuid4().hex[:12]}", snapshot_id, tool_call_id, field_name,
         field_value, now_iso()))


def list_citations(snapshot_id: str) -> list[dict[str, Any]]:
    rows = get_db().execute(
        "SELECT * FROM self_model_citations WHERE snapshot_id = ? ORDER BY id",
        (snapshot_id,)).fetchall()
    return [dict(r) for r in rows]


# --- goals -------------------------------------------------------------------

def declare_goal(*, scope_kind: str, scope_ref: str, goal_text: str,
                 source_turn_text: str | None = None) -> dict[str, Any]:
    """What Jarvis understands the goal to be — recorded as its own reading,
    never as a verified account of what the user meant. `source_turn_text` is the
    user's actual words at the time, so drift can later be judged against what
    was really said rather than against a paraphrase."""
    goal_id = f"goal_{uuid.uuid4().hex[:12]}"
    get_db().execute(
        "INSERT INTO self_goals (id, scope_kind, scope_ref, goal_text, declared_at, "
        "status, source_turn_text) VALUES (?, ?, ?, ?, ?, 'active', ?)",
        (goal_id, scope_kind, scope_ref, goal_text, now_iso(), source_turn_text))
    return get_goal(goal_id)  # type: ignore[return-value]


def get_goal(goal_id: str) -> dict[str, Any] | None:
    row = get_db().execute("SELECT * FROM self_goals WHERE id = ?", (goal_id,)).fetchone()
    return dict(row) if row else None


def get_active_goal(scope_kind: str, scope_ref: str) -> dict[str, Any] | None:
    row = get_db().execute(
        "SELECT * FROM self_goals WHERE scope_kind = ? AND scope_ref = ? AND status = 'active' "
        "ORDER BY declared_at DESC LIMIT 1", (scope_kind, scope_ref)).fetchone()
    return dict(row) if row else None


def close_goal(goal_id: str) -> None:
    get_db().execute("UPDATE self_goals SET status = 'closed', closed_at = ? WHERE id = ?",
                     (now_iso(), goal_id))
