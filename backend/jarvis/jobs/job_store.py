"""Jobs, their write-ahead trace, and the outbox that reaches the user.

**The trace is what makes crash recovery honest rather than declared.** Every
effectful action writes an `intent` row BEFORE it runs and an `outcome` row
after, so a crash between the two still leaves the intent on record — and
`policy.classify_recovery()` can tell "nothing happened" from "something might
have" without asking the job to assess itself.

The outbox is the interruption broker: a row is the durable record that the user
needs to decide something. A background job may wait hours, so the record has to
outlive any short-lived token — which is exactly why it is a table.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from ..db import get_db
from ..jscompat import now_iso

ACTIVE_STATUSES = ("queued", "running", "awaiting_decision")


def _row(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    job = {
        "id": row["id"], "parentId": row["parent_id"],
        "conversationId": row["conversation_id"], "title": row["title"],
        "goal": row["goal"], "kind": row["kind"], "status": row["status"],
        "plan": json.loads(row["plan"]) if row["plan"] else None,
        "resource": row["resource"], "recovery": row["recovery"],
        "result": row["result"], "error": row["error"], "retries": row["retries"],
        "createdAt": row["created_at"], "startedAt": row["started_at"],
        "heartbeatAt": row["heartbeat_at"], "finishedAt": row["finished_at"],
        "priority": row["priority"], "progress": row["progress"],
        "currentStep": row["current_step"],
    }
    return job


def create_job(*, title: str, goal: str, kind: str = "generic",
               conversation_id: str | None = None, parent_id: str | None = None,
               plan: dict[str, Any] | None = None, resource: str | None = None,
               priority: int = 2, status: str = "queued") -> dict[str, Any]:
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    get_db().execute(
        "INSERT INTO jobs (id, parent_id, conversation_id, title, goal, kind, status, plan, "
        "resource, retries, created_at, priority) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
        (job_id, parent_id, conversation_id, title, goal, kind, status,
         json.dumps(plan) if plan else None, resource, now_iso(), priority))
    return get_job(job_id)  # type: ignore[return-value]


def get_job(job_id: str) -> dict[str, Any] | None:
    return _row(get_db().execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())


def list_jobs(status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    if status:
        rows = get_db().execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY priority, created_at DESC LIMIT ?",
            (status, limit)).fetchall()
    else:
        rows = get_db().execute(
            "SELECT * FROM jobs ORDER BY priority, created_at DESC LIMIT ?", (limit,)).fetchall()
    return [_row(r) for r in rows]  # type: ignore[misc]


def list_active_jobs() -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    rows = get_db().execute(
        f"SELECT * FROM jobs WHERE status IN ({placeholders}) ORDER BY priority, created_at",
        ACTIVE_STATUSES).fetchall()
    return [_row(r) for r in rows]  # type: ignore[misc]


_PATCH_COLUMNS = {
    "status": "status", "result": "result", "error": "error", "retries": "retries",
    "recovery": "recovery", "startedAt": "started_at", "heartbeatAt": "heartbeat_at",
    "finishedAt": "finished_at", "progress": "progress", "currentStep": "current_step",
    "priority": "priority", "resource": "resource", "title": "title",
}


def update_job(job_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    columns = [(_PATCH_COLUMNS[k], v) for k, v in patch.items() if k in _PATCH_COLUMNS]
    if columns:
        assignments = ", ".join(f"{name} = ?" for name, _ in columns)
        get_db().execute(f"UPDATE jobs SET {assignments} WHERE id = ?",
                         [value for _, value in columns] + [job_id])
    return get_job(job_id)


def heartbeat(job_id: str, *, step: str | None = None,
              progress: int | None = None) -> None:
    """Say the job is still alive, and optionally what it is doing right now.

    Progress stays NULL unless the work actually reports one: a job that cannot
    say how far along it is must not be shown as 0%, which reads as stuck.
    """
    patch: dict[str, Any] = {"heartbeatAt": now_iso()}
    if step is not None:
        patch["currentStep"] = step
    if progress is not None:
        patch["progress"] = max(0, min(100, int(progress)))
    update_job(job_id, patch)


# --- the write-ahead trace ---------------------------------------------------

#: The trace table is shared: a row belongs to a `source` (jobs here, and later
#: operational checks and verification) with its own independent sequence, so
#: one subsystem's numbering can never be perturbed by another's writes.
SOURCE = "job"


def append_trace(job_id: str, *, phase: str, effect: str, kind: str, summary: str,
                 detail: Any = None) -> None:
    """`phase` is 'intent' or 'outcome'; `effect` is 'read', 'workspace' or
    'external'. The effect is what recovery classification reads, so it is
    recorded by the caller that knows what the action actually touches."""
    db = get_db()
    row = db.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM trace WHERE source = ? AND source_ref = ?",
        (SOURCE, job_id)).fetchone()
    db.execute(
        "INSERT INTO trace (source, source_ref, job_id, seq, phase, effect, kind, summary, "
        "detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (SOURCE, job_id, job_id, row["next"], phase, effect, kind, summary,
         json.dumps(detail, default=str) if detail is not None else None, now_iso()))


def get_trace(job_id: str) -> list[dict[str, Any]]:
    rows = get_db().execute(
        "SELECT * FROM trace WHERE source = ? AND source_ref = ? ORDER BY seq",
        (SOURCE, job_id)).fetchall()
    return [dict(r) for r in rows]


def get_trace_tail(job_id: str, size: int) -> list[dict[str, Any]]:
    rows = get_db().execute(
        "SELECT * FROM trace WHERE source = ? AND source_ref = ? ORDER BY seq DESC LIMIT ?",
        (SOURCE, job_id, size)).fetchall()
    return [dict(r) for r in reversed(rows)]


# --- the outbox --------------------------------------------------------------

def add_outbox(*, tier: int, summary: str, job_id: str | None = None,
               source: str = "job", source_ref: str | None = None,
               reason: str = "permission", detail: Any = None) -> int:
    cursor = get_db().execute(
        "INSERT INTO outbox (source, source_ref, job_id, tier, reason, summary, detail, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (source, source_ref or job_id, job_id, tier, reason, summary,
         json.dumps(detail, default=str) if detail is not None else None, now_iso()))
    return int(cursor.lastrowid or 0)


def list_pending_outbox(max_tier: int = 2) -> list[dict[str, Any]]:
    """Undelivered rows worth interrupting for. Tier 3 is pull-only and never
    appears here — nothing about it justifies breaking into a conversation."""
    rows = get_db().execute(
        "SELECT * FROM outbox WHERE delivered_at IS NULL AND tier <= ? "
        "ORDER BY tier, created_at", (max_tier,)).fetchall()
    return [dict(r) for r in rows]


def mark_delivered(outbox_id: int) -> None:
    """Marked only by the action that actually RESOLVES the decision, never by
    showing it — a turn that fails before the model replies must not lose it."""
    get_db().execute("UPDATE outbox SET delivered_at = ? WHERE id = ?", (now_iso(), outbox_id))


def deliver_all_for_job(job_id: str) -> None:
    get_db().execute(
        "UPDATE outbox SET delivered_at = ? WHERE job_id = ? AND delivered_at IS NULL",
        (now_iso(), job_id))
