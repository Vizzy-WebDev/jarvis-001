"""The interruption broker: things waiting to reach the user, by tier.

One table for every source — a background job needing permission, a heartbeat
finding, a verification mismatch — because they are the same thing: something
that needs a person, held until there is a way to reach them that is not an
interruption in itself.

Tiers are structural, not timed:

* **1** — needs them now. Eligible for real proactive speech, if they are
  actually reachable.
* **2** — worth saying at a natural moment. Delivered into a turn they started.
* **3** — worth a record only. Never surfaces on its own; pull only.

A row is marked delivered by the action that RESOLVES it, never by showing it.
A turn that fails after the model saw a notice must not lose it.
"""

from __future__ import annotations

import json
from typing import Any

from ..db import get_db
from ..jscompat import now_iso


def _entry(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"], "source": row["source"], "sourceRef": row["source_ref"],
        "jobId": row["job_id"], "tier": row["tier"], "reason": row["reason"],
        "summary": row["summary"],
        "detail": json.loads(row["detail"]) if row["detail"] else None,
        "confirmPayload": json.loads(row["confirm_payload"]) if row["confirm_payload"] else None,
        "createdAt": row["created_at"], "deliveredAt": row["delivered_at"],
    }


def add(*, tier: int, summary: str, source: str = "heartbeat",
        source_ref: str | None = None, job_id: str | None = None,
        reason: str = "notice", detail: Any = None,
        confirm_payload: Any = None) -> int:
    if not summary:
        raise ValueError("an outbox entry needs a summary")
    cursor = get_db().execute(
        "INSERT INTO outbox (source, source_ref, job_id, tier, reason, summary, detail, "
        "confirm_payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (source, source_ref or job_id, job_id, tier, reason, summary,
         json.dumps(detail, default=str) if detail is not None else None,
         json.dumps(confirm_payload, default=str) if confirm_payload is not None else None,
         now_iso()))
    return int(cursor.lastrowid or 0)


def list_pending(max_tier: int = 2, source: str | None = None) -> list[dict[str, Any]]:
    """Undelivered rows worth interrupting for. Tier 3 never appears: nothing
    about it justifies breaking into a conversation."""
    clauses = ["delivered_at IS NULL", "tier <= ?"]
    params: list[Any] = [max_tier]
    if source is not None:
        clauses.append("source = ?")
        params.append(source)
    rows = get_db().execute(
        f"SELECT * FROM outbox WHERE {' AND '.join(clauses)} ORDER BY tier, created_at",
        tuple(params)).fetchall()
    return [_entry(r) for r in rows]


def get(entry_id: int) -> dict[str, Any] | None:
    row = get_db().execute("SELECT * FROM outbox WHERE id = ?", (entry_id,)).fetchone()
    return _entry(row) if row is not None else None


def pending_for_source_ref(source: str, source_ref: str | None) -> list[dict[str, Any]]:
    """The duplicate check a source runs before parking a second row for the same
    finding."""
    rows = get_db().execute(
        "SELECT * FROM outbox WHERE source = ? AND source_ref = ? AND delivered_at IS NULL "
        "ORDER BY created_at", (source, source_ref)).fetchall()
    return [_entry(r) for r in rows]


def for_job(job_id: str) -> list[dict[str, Any]]:
    rows = get_db().execute(
        "SELECT * FROM outbox WHERE job_id = ? ORDER BY created_at", (job_id,)).fetchall()
    return [_entry(r) for r in rows]


def mark_delivered(entry_id: int) -> None:
    get_db().execute("UPDATE outbox SET delivered_at = ? WHERE id = ?", (now_iso(), entry_id))


def deliver_all_for_job(job_id: str) -> None:
    get_db().execute("UPDATE outbox SET delivered_at = ? WHERE job_id = ? AND delivered_at IS NULL",
                     (now_iso(), job_id))
