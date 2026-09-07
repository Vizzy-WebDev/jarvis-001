"""Every steady-state decision about a background job, as pure functions.

Zero imports, deliberately — the same discipline as the memory policy. These run
on every supervisor tick for every active job, so "no model calls, no side
effects, no I/O" is the actual cost budget this file exists to hold to, and it is
what lets the whole thing be checked as a truth table.

Not here on purpose: admission planning (decomposing a goal, picking a worker
kind) and anything needing live tool metadata. Those are rare, boundary-only
decisions; this is the continuous, free part.
"""

from __future__ import annotations

import json
from typing import Any

DIAGNOSE_TAIL_SIZE = 8

#: A deterministic step budget per kind — the backstop for a worker that never
#: repeats itself and never converges either.
STEP_BUDGET_BY_KIND = {"research": 30, "files": 20, "computer": 25, "generic": 30}

NEAR_DUPLICATE_SIMILARITY = 0.85


# --- crash recovery ----------------------------------------------------------

def classify_recovery(job: dict[str, Any] | None, trace: list[dict[str, Any]] | None = None) -> str:
    """An honest verdict for a job found `running` with nothing running it.

    Pessimistic at every branch, on purpose:

    - Parked on a decision when it died → it still needs that decision.
    - ANY trace row marked `effect: 'external'` → unrecoverable, whether the
      action completed or only got as far as being logged as about to happen. A
      crash between logging the intent and doing the thing is indistinguishable
      from one between doing it and logging the outcome, so the write-ahead
      intent alone is enough to condemn it. Retrying risks doing it twice.
    - Nothing beyond reads → safe to pick back up.
    - Otherwise (touched its own workspace, nothing external) → safe to start
      over, since nothing irreversible happened outside the job's own scope.
    """
    trace = trace or []
    if (job or {}).get("status") == "awaiting_decision":
        return "needs_input"
    if any(row.get("effect") == "external" for row in trace):
        return "unrecoverable"
    if not any(row.get("effect") == "workspace" for row in trace):
        return "resumable"
    return "restartable"


# --- live stall diagnosis ----------------------------------------------------

def _stable(value: Any) -> str:
    """Order-independent JSON, so `{a:1,b:2}` and `{b:2,a:1}` are one signature."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _detail(row: dict[str, Any]) -> Any:
    raw = row.get("detail")
    if not raw:
        return None
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None


def _tool_signature(row: dict[str, Any]) -> str | None:
    detail = _detail(row)
    if not isinstance(detail, dict) or not detail.get("name"):
        return None
    return f"{detail['name']}:{_stable(detail.get('args') or {})}"


def _tool_outcome(row: dict[str, Any]) -> dict[str, Any] | None:
    detail = _detail(row)
    if isinstance(detail, dict) and detail.get("name") and "ok" in detail:
        return detail
    return None


def text_similarity(a: str, b: str) -> float:
    """Dice coefficient over character bigrams — cheap, dependency-free, and
    good enough to catch a model restating itself near-verbatim."""
    def bigrams(text: str) -> dict[str, int]:
        norm = " ".join(str(text or "").lower().split())
        counts: dict[str, int] = {}
        for i in range(len(norm) - 1):
            pair = norm[i:i + 2]
            counts[pair] = counts.get(pair, 0) + 1
        return counts

    left, right = bigrams(a), bigrams(b)
    if not left or not right:
        return 0.0
    shared = sum(min(count, right.get(pair, 0)) for pair, count in left.items())
    return (2 * shared) / (sum(left.values()) + sum(right.values()))


def _repeats_with_period(signatures: list[str | None], period: int, cycles: int) -> bool:
    window = period * cycles
    if len(signatures) < window:
        return False
    tail = signatures[-window:]
    # An unrecognisable entry never counts as a match: a cycle "proven" by two
    # blanks is not a cycle.
    return all(tail[i] is not None and tail[i] == tail[i % period] for i in range(window))


def diagnose_stall(tail: list[dict[str, Any]] | None = None) -> dict[str, str] | None:
    """Is this job alive, still emitting events, and going nowhere?

    Heartbeat silence catches a hung or dead process. This catches the other
    failure: a worker calling tools on schedule and making no progress. Checked
    in order, first match wins.
    """
    tail = tail or []
    intents = [row for row in tail if row.get("kind") == "tool" and row.get("phase") == "intent"]
    signatures = [_tool_signature(row) for row in intents]

    last_three = signatures[-3:]
    if len(last_three) == 3 and last_three[0] is not None and len(set(last_three)) == 1:
        return {"cause": "exact_repeat",
                "detail": f"Repeated {last_three[0]} three times with no new result."}

    for period in (2, 3):
        cycles = 3 if period == 2 else 2
        if _repeats_with_period(signatures, period, cycles):
            pattern = " -> ".join(str(s) for s in signatures[-period:])
            return {"cause": "oscillation",
                    "detail": f"Cycling between the same {period} actions ({pattern}) "
                              "without new progress."}

    outcomes = [_tool_outcome(row) for row in tail
                if row.get("kind") == "tool" and row.get("phase") == "outcome"]
    last_outcomes = outcomes[-3:]
    if len(last_outcomes) == 3 and all(o and o.get("ok") is False for o in last_outcomes):
        return {"cause": "repeated_failure",
                "detail": "The last three attempts all failed, none of them a different "
                          "approach that worked."}

    notes = [row for row in tail if row.get("kind") == "note"]
    if len(notes) >= 2:
        first, second = notes[-2], notes[-1]
        similarity = text_similarity(first.get("detail") or first.get("summary") or "",
                                     second.get("detail") or second.get("summary") or "")
        if similarity >= NEAR_DUPLICATE_SIMILARITY:
            # The one failure mode with no tool calls at all, so nothing above
            # can see it.
            return {"cause": "near_duplicate_reasoning",
                    "detail": "The last two reasoning steps say essentially the same thing "
                              "— no tool calls, no new direction."}
    return None


def step_budget_exceeded(step_count: int, kind: str) -> bool:
    return step_count >= STEP_BUDGET_BY_KIND.get(kind, STEP_BUDGET_BY_KIND["generic"])


# --- steady-state supervision ------------------------------------------------

def is_hung(job: dict[str, Any] | None, *, now_ms: float, timeout_ms: float) -> bool:
    """Genuine silence, not "thinking hard": no heartbeat for longer than the
    timeout on a job that claims to be running."""
    if (job or {}).get("status") != "running" or not (job or {}).get("heartbeatAt"):
        return False
    from datetime import datetime

    try:
        beat = datetime.fromisoformat(str(job["heartbeatAt"]).replace("Z", "+00:00"))
    except ValueError:
        return False
    return (now_ms - beat.timestamp() * 1000) > timeout_ms


def has_capacity(active_jobs: list[Any], max_jobs: int) -> bool:
    """Never silently queue past this. The caller reports being at capacity and
    asks the user what should give way — a queue nobody can see is worse than a
    refusal they can answer."""
    return len(active_jobs) < max_jobs


def resource_available(active_jobs: list[dict[str, Any]], resource: str | None) -> bool:
    """A resource (the desktop, say) may be held by at most one active job."""
    if not resource:
        return True
    return not any(job.get("resource") == resource for job in active_jobs)


def can_auto_retry(job: dict[str, Any] | None) -> bool:
    """One automatic attempt, full stop — a crash retry and a stall retry share
    the same counter, so a job cannot get two goes by failing in two ways."""
    return ((job or {}).get("retries") or 0) == 0
