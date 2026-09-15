"""Durable storage for cost tracking — three tables, one module.

A leaf: imports the database and the time helper, nothing else. That is what
makes it safe for a tool to import directly (`jarvis/tools/` may not reach the
loader, the executor or the gateway) and what keeps a recording failure
confined to recording.

`session_id` is deliberately NOT a foreign key, the same discipline
`jobs.conversation_id` already uses: a cost event has to survive the
conversation that generated it being deleted, or the month's total quietly
shrinks whenever the user tidies up their chat history.
"""

from __future__ import annotations

import json
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


# --- provider-reported balances ----------------------------------------------

def record_balance(provider_ref: str, detail: dict[str, Any] | None) -> None:
    """The latest reading only — no history. An old balance is not a fact about
    now, and keeping a trail of them invites presenting a stale one as current."""
    if not provider_ref:
        raise ValueError("a balance reading needs a provider ref")
    get_db().execute(
        "INSERT INTO provider_balances (provider_ref, checked_at, detail_json) VALUES (?, ?, ?) "
        "ON CONFLICT(provider_ref) DO UPDATE SET checked_at = excluded.checked_at, "
        "detail_json = excluded.detail_json",
        (provider_ref, now_iso(), json.dumps(detail or {}, default=str)))


def get_balance(provider_ref: str) -> dict[str, Any] | None:
    row = get_db().execute(
        "SELECT * FROM provider_balances WHERE provider_ref = ?", (provider_ref,)).fetchone()
    if row is None:
        return None
    return {"providerRef": row["provider_ref"], "checkedAt": row["checked_at"],
            "detail": json.loads(row["detail_json"])}


def list_balances() -> list[dict[str, Any]]:
    rows = get_db().execute(
        "SELECT * FROM provider_balances ORDER BY provider_ref ASC").fetchall()
    return [{"providerRef": r["provider_ref"], "checkedAt": r["checked_at"],
             "detail": json.loads(r["detail_json"])} for r in rows]


# --- prices ------------------------------------------------------------------

def set_price(*, provider: str, model_id: str, unit_kind: str = "tokens",
              price_in: float | None = None, price_out: float | None = None,
              currency: str = "USD", source: str = "built_in") -> None:
    """`source` is 'user' | 'provider_reported' | 'built_in' and is descriptive
    only: precedence is decided by WHICH caller writes a row, in prices.py, not
    by comparing this field at read time."""
    if not provider or not model_id or not unit_kind:
        raise ValueError("a price needs a provider, a model id and a unit kind")
    get_db().execute(
        "INSERT INTO model_prices (provider, model_id, unit_kind, price_in, price_out, "
        "currency, source, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(provider, model_id, unit_kind) DO UPDATE SET "
        "price_in = excluded.price_in, price_out = excluded.price_out, "
        "currency = excluded.currency, source = excluded.source, updated_at = excluded.updated_at",
        (provider, model_id, unit_kind, price_in, price_out, currency, source, now_iso()))


def _price(row: Any) -> dict[str, Any]:
    return {"provider": row["provider"], "modelId": row["model_id"],
            "unitKind": row["unit_kind"], "priceIn": row["price_in"],
            "priceOut": row["price_out"], "currency": row["currency"],
            "source": row["source"], "updatedAt": row["updated_at"]}


def get_price(provider: str, model_id: str | None,
              unit_kind: str = "tokens") -> dict[str, Any] | None:
    if not provider or not model_id:
        return None
    row = get_db().execute(
        "SELECT * FROM model_prices WHERE provider = ? AND model_id = ? AND unit_kind = ?",
        (provider, model_id, unit_kind)).fetchone()
    return _price(row) if row is not None else None


def list_prices() -> list[dict[str, Any]]:
    rows = get_db().execute(
        "SELECT * FROM model_prices ORDER BY provider ASC, model_id ASC").fetchall()
    return [_price(r) for r in rows]
