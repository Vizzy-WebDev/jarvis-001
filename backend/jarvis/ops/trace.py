"""The write-ahead activity trace, shared by every subsystem that needs one.

An `intent` row goes down BEFORE an effectful action and an `outcome` row after.
A crash between the two therefore still leaves the intent on record, with what
it was about to touch — which is the entire basis on which recovery can be
honest rather than declared. Jobs proved the shape; nothing about it is
job-specific, so it lives here and jobs uses it.

`seq` is scoped to `(source, source_ref)`, so one subsystem's numbering can never
be perturbed by another's writes — a diagnosis check and a running job write into
the same table without either being able to disturb the other's sequence.
"""

from __future__ import annotations

import json
from typing import Any

from ..db import get_db
from ..jscompat import now_iso

#: 'intent' before, 'outcome' after.
PHASES = ("intent", "outcome")
#: What the action touches, and the only thing recovery classification reads.
#: 'external' means it reached outside this machine's own workspace and may not
#: be safely repeatable.
EFFECTS = ("read", "workspace", "external")


def append(*, source: str, source_ref: str | None, phase: str, effect: str,
           kind: str, summary: str, detail: Any = None,
           job_id: str | None = None) -> int:
    if phase not in PHASES:
        raise ValueError(f"a trace phase is 'intent' or 'outcome', not {phase!r}")
    if effect not in EFFECTS:
        raise ValueError(f"a trace effect is one of {EFFECTS}, not {effect!r}")
    db = get_db()
    row = db.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM trace WHERE source = ? AND source_ref = ?",
        (source, source_ref)).fetchone()
    cursor = db.execute(
        "INSERT INTO trace (source, source_ref, job_id, seq, phase, effect, kind, summary, "
        "detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (source, source_ref, job_id, row["next"], phase, effect, kind, summary,
         json.dumps(detail, default=str) if detail is not None else None, now_iso()))
    return int(cursor.lastrowid or 0)


def read(source: str, source_ref: str | None) -> list[dict[str, Any]]:
    rows = get_db().execute(
        "SELECT * FROM trace WHERE source = ? AND source_ref = ? ORDER BY seq",
        (source, source_ref)).fetchall()
    return [dict(r) for r in rows]


def tail(source: str, source_ref: str | None, size: int) -> list[dict[str, Any]]:
    rows = get_db().execute(
        "SELECT * FROM trace WHERE source = ? AND source_ref = ? ORDER BY seq DESC LIMIT ?",
        (source, source_ref, size)).fetchall()
    return [dict(r) for r in reversed(rows)]


def recent(source: str | None = None, *, since_iso: str | None = None,
           limit: int = 200) -> list[dict[str, Any]]:
    """The latest rows across every ref — what "has anything gone wrong with you
    lately" is answered from."""
    clauses, params = [], []
    if source is not None:
        clauses.append("source = ?")
        params.append(source)
    if since_iso is not None:
        clauses.append("created_at >= ?")
        params.append(since_iso)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    rows = get_db().execute(
        f"SELECT * FROM trace {where} ORDER BY id DESC LIMIT ?", tuple(params)).fetchall()
    return [dict(r) for r in rows]
