"""Usage tracking (§17) — one row per attempted call, success or failure.

`ai_usage` is the per-request ledger: enough to answer "what did this cost,"
"how fast was it," and, joined with a `fallback_chain`, "why did it take three
tries" (§30's routing trace reads straight off this). `ai/fallback.py` writes
one row per ATTEMPT, not one per request — a request that failed twice before
succeeding leaves three rows, because each attempt genuinely happened and
genuinely has its own latency and its own outcome.

**A number that was not measured is absent, never zero.** Zero is a claim
("this cost nothing"); absence is "nothing was reported." `speed_tier()`
answers `None` until enough real calls have completed, and the router treats
`None` as neutral — never invents a number to rank on.
"""

from __future__ import annotations

import json
import statistics
from typing import Any

from ..db import get_db
from ..jscompat import now_iso
from .request import Usage

#: Recent time-to-first-token buckets, in milliseconds. Higher tier = faster.
#: Authored thresholds, not a measurement — they exist to turn a continuous
#: number into the same small ranking domain `quality`/cost already use.
_SPEED_BUCKETS: tuple[tuple[int, int], ...] = ((400, 5), (800, 4), (1500, 3), (3000, 2))
#: How many of a model's most recent calls `speed_tier` looks at. Recent
#: enough to reflect current conditions, not so few that one slow network
#: blip swings the whole ranking.
SPEED_WINDOW = 20


def record(
    *,
    request_id: str,
    model_id: str | None,
    provider_id: str | None,
    role: str | None,
    success: bool,
    usage: Usage | None = None,
    cost_estimate: float | None = None,
    latency_ms: int | None = None,
    ttft_ms: int | None = None,
    error_type: str | None = None,
    fallback_chain: list[dict[str, Any]] | None = None,
    session_id: str | None = None,
    background: bool = False,
) -> int:
    usage = usage or Usage()
    cursor = get_db().execute(
        "INSERT INTO ai_usage (request_id, ts, provider_id, model_id, role, tokens_in, "
        "tokens_out, tokens_reasoning, cached_in, cost_estimate, latency_ms, ttft_ms, "
        "success, error_type, fallback_chain_json, session_id, background) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (request_id, now_iso(), provider_id, model_id, role, usage.tokens_in, usage.tokens_out,
         usage.tokens_reasoning, usage.cached_in, cost_estimate, latency_ms, ttft_ms,
         1 if success else 0, error_type,
         json.dumps(fallback_chain) if fallback_chain is not None else None,
         session_id, 1 if background else 0),
    )
    return int(cursor.lastrowid or 0)


def _row(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"], "requestId": row["request_id"], "ts": row["ts"],
        "providerId": row["provider_id"], "modelId": row["model_id"], "role": row["role"],
        "tokensIn": row["tokens_in"], "tokensOut": row["tokens_out"],
        "tokensReasoning": row["tokens_reasoning"], "cachedIn": row["cached_in"],
        "costEstimate": row["cost_estimate"], "latencyMs": row["latency_ms"],
        "ttftMs": row["ttft_ms"], "success": bool(row["success"]), "errorType": row["error_type"],
        "fallbackChain": json.loads(row["fallback_chain_json"]) if row["fallback_chain_json"] else None,
        "sessionId": row["session_id"], "background": bool(row["background"]),
    }


def for_request(request_id: str) -> list[dict[str, Any]]:
    """Every attempt recorded for one request, in order — the routing trace
    §30 asks for: request -> routing decision -> execution -> retry ->
    fallback -> final result, reconstructed from real rows."""
    rows = get_db().execute(
        "SELECT * FROM ai_usage WHERE request_id = ? ORDER BY id ASC", (request_id,)).fetchall()
    return [_row(r) for r in rows]


def recent_for_model(model_id: str, limit: int = 500) -> list[dict[str, Any]]:
    rows = get_db().execute(
        "SELECT * FROM ai_usage WHERE model_id = ? ORDER BY id DESC LIMIT ?",
        (model_id, limit)).fetchall()
    return [_row(r) for r in reversed(rows)]


def speed_tier(model_id: str) -> int | None:
    """The measured time-to-first-token bucket for this model, or `None` if
    nothing has been measured yet. Time to FIRST token, not total duration —
    total duration mostly measures how long the reply was, which would rank
    a model by the shape of recent questions rather than anything about the
    model itself."""
    rows = get_db().execute(
        "SELECT ttft_ms FROM ai_usage WHERE model_id = ? AND success = 1 AND ttft_ms IS NOT NULL "
        "ORDER BY id DESC LIMIT ?", (model_id, SPEED_WINDOW)).fetchall()
    values = [r["ttft_ms"] for r in rows if isinstance(r["ttft_ms"], (int, float))]
    if not values:
        return None
    median = statistics.median(values)
    for ceiling, tier in _SPEED_BUCKETS:
        if median <= ceiling:
            return tier
    return 1


def summary_since(since_iso: str) -> list[dict[str, Any]]:
    """Grouped by (provider, model): call count, success rate, total tokens,
    total cost where known — the raw material for a usage screen."""
    rows = get_db().execute(
        "SELECT provider_id, model_id, "
        "COUNT(*) AS calls, SUM(success) AS successes, "
        "SUM(COALESCE(tokens_in, 0)) AS tokens_in, SUM(COALESCE(tokens_out, 0)) AS tokens_out, "
        "SUM(cost_estimate) AS cost, AVG(latency_ms) AS avg_latency_ms "
        "FROM ai_usage WHERE ts >= ? GROUP BY provider_id, model_id "
        "ORDER BY calls DESC", (since_iso,)).fetchall()
    return [{
        "providerId": r["provider_id"], "modelId": r["model_id"], "calls": r["calls"],
        "successes": r["successes"], "tokensIn": r["tokens_in"], "tokensOut": r["tokens_out"],
        "cost": r["cost"], "avgLatencyMs": r["avg_latency_ms"],
    } for r in rows]
