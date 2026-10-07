"""Durable storage for cost tracking — three tables, one module.

A leaf: imports the database and the time helper, nothing else. That is what
makes it safe for a tool to import directly (`jarvis/tools/` may not reach the
loader or the executor) and what keeps a recording failure
confined to recording.

`session_id` is deliberately NOT a foreign key, the same discipline
`jobs.conversation_id` already uses: a cost event has to survive the
conversation that generated it being deleted, or the month's total quietly
shrinks whenever the user tidies up their chat history.
"""

from __future__ import annotations

from typing import Any

from ..db import get_db
from ..jscompat import now_iso


def _event(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"], "ts": row["ts"], "provider": row["provider"],
        "modelId": row["model_id"], "unitKind": row["unit_kind"],
        "unitsIn": row["units_in"], "unitsOut": row["units_out"],
        "cachedIn": row["cached_in"], "sessionId": row["session_id"],
        "background": bool(row["background"]),
    }


# --- measured usage ----------------------------------------------------------

def record_event(*, provider: str, unit_kind: str, model_id: str | None = None,
                 units_in: int | None = None, units_out: int | None = None,
                 cached_in: int | None = None, session_id: str | None = None,
                 background: bool = False) -> int:
    """One real unit of usage. `unit_kind` is 'tokens', 'characters', 'seconds'
    or 'requests' — a service with no in/out split passes only `units_out`,
    "the thing produced"."""
    if not provider or not unit_kind:
        raise ValueError("a cost event needs a provider and a unit kind")
    cursor = get_db().execute(
        "INSERT INTO cost_events (ts, provider, model_id, unit_kind, units_in, units_out, "
        "cached_in, session_id, background) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (now_iso(), provider, model_id, unit_kind, units_in, units_out, cached_in,
         session_id, 1 if background else 0))
    return int(cursor.lastrowid or 0)


def list_events_since(since_iso: str) -> list[dict[str, Any]]:
    rows = get_db().execute(
        "SELECT * FROM cost_events WHERE ts >= ? ORDER BY ts ASC", (since_iso,)).fetchall()
    return [_event(r) for r in rows]


def list_events_for_model(provider: str, model_id: str | None,
                          limit: int = 500) -> list[dict[str, Any]]:
    db = get_db()
    if model_id:
        rows = db.execute(
            "SELECT * FROM cost_events WHERE provider = ? AND model_id = ? "
            "ORDER BY ts DESC LIMIT ?", (provider, model_id, limit)).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM cost_events WHERE provider = ? AND model_id IS NULL "
            "ORDER BY ts DESC LIMIT ?", (provider, limit)).fetchall()
    return [_event(r) for r in reversed(rows)]
