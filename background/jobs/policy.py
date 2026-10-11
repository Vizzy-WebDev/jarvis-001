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

def classify_recovery(job: dict[str, Any] | None, trace: list[dict[str, Any]] | None = None,
                      recorded: set[str] | None = None) -> str:
    """An honest verdict for a job found `running` with nothing running it.

    The work is durable: its finished rounds are saved, and an action that finished is
    recorded under an operation id a replay reuses, so picking it back up never repeats it.
    What is left to judge is the one action that might have been in flight:

    - Parked on a decision when it died → it still needs that decision (`needs_input`).
    - An action that reaches outside Jarvis (`effect: 'external'`) STARTED and has no
      outcome — neither an outcome row nor a `recorded` result (operation ids that finished)
      → `unrecoverable`: it may or may not have happened, and only a person can check.
      Retrying it unasked risks doing it twice.
    - Otherwise → `resumable`: it continues from its last finished round.
    """
    trace = trace or []
    if (job or {}).get("status") == "awaiting_decision":
        return "needs_input"
    if open_external_intents(trace, recorded):
        return "unrecoverable"
    return "resumable"


def open_external_intents(trace: list[dict[str, Any]] | None,
                          recorded: set[str] | None = None) -> list[dict[str, Any]]:
    """External actions that started and never recorded an outcome: `{name, args,
    operationId}` each, oldest first. An intent with no operation id cannot be matched to an
    outcome, so it counts as open — the pessimistic reading."""
    trace = trace or []
    recorded = recorded or set()
    finished = {(_detail(row) or {}).get("operationId") for row in trace
                if row.get("phase") == "outcome" and isinstance(_detail(row), dict)}
    out = []
    for row in trace:
        if row.get("phase") != "intent" or row.get("kind") != "tool" \
                or row.get("effect") != "external":
            continue
        detail = _detail(row)
        detail = detail if isinstance(detail, dict) else {}
        operation = detail.get("operationId")
        if operation and (operation in finished or operation in recorded):
            continue
        out.append({"name": detail.get("name"), "args": detail.get("args") or {},
                    "operationId": operation})
    return out


def has_external_effect(trace: list[dict[str, Any]] | None) -> bool:
    """Did anything this job did — or started to do — reach outside Jarvis? Starting over
    would do it again, which is what makes a restart need the person's say-so."""
    return any(row.get("effect") == "external" for row in (trace or []))


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
    # Only what happened since the work was last nudged (a retry, the person's guidance):
    # the rows before it are the very pattern that nudge already answered.
    nudges = [i for i, row in enumerate(tail) if row.get("kind") == "nudge"]
    if nudges:
        tail = tail[nudges[-1] + 1:]
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
    """Whether a job has never been set going again. Kept for readers that only ask that;
    whether it MAY be is `may_recover`, which counts by cause."""
    return ((job or {}).get("retries") or 0) == 0


# --- setting work going again, by why it stopped ----------------------------------------------
#
# One automatic retry shared by every kind of trouble was the old rule, and on the owner's
# install it left both jobs ever run "stuck": the single retry went on the first hiccup and
# the next one — a different problem entirely — went straight to the person. Three different
# things stop work, and each gets its own short allowance:

#: `approach` — it is going round in circles or its round failed: told why, and to take a
#: DIFFERENT way. `check` — it finished, the result was checked against what was asked and
#: sent back with the reason. `crash` — it hung or died. (No model being able to answer is none
#: of these: the work simply waits — `durable.model_wait`.)
RECOVERY_LIMITS = {"approach": 2, "check": 2, "crash": 2}


def recovery_kind(cause: str | None) -> str:
    cause = str(cause or "")
    if cause == "did not match the request":
        return "check"
    if cause in ("hung", "crashed"):
        return "crash"
    return "approach"


def may_recover(job: dict[str, Any] | None, kind: str) -> bool:
    spent = ((job or {}).get("attempts") or {}).get(kind) or 0
    return spent < RECOVERY_LIMITS.get(kind, 0)


def holds_a_slot(job: dict[str, Any] | None, *, live: bool, waiting_for_model: bool) -> bool:
    """Whether a job counts against how many may work at once: it is running, about to (a
    worker has it), or only paused until a model can answer. A job waiting on the PERSON does
    not — an unanswered question must not stop other work from starting — and neither does one
    still in line."""
    status = (job or {}).get("status")
    return status == "running" or (status == "queued" and (live or waiting_for_model))


def next_in_line(waiting: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The order work in line is started in: most important first, then first come."""
    return sorted(waiting, key=lambda job: (job.get("priority") or 2, str(job.get("createdAt") or "")))


# --- a step allowance that follows the work ---------------------------------------------------

#: How many times a job's step allowance is extended by itself when it runs out while making
#: real progress. Past that — or with no progress — the person is asked.
MAX_AUTO_EXTENSIONS = 3
#: This many more distinct successful actions than at the last mark is progress by itself.
PROGRESS_CALLS = 3


def made_progress(before: dict[str, Any] | None, now: dict[str, Any] | None) -> bool:
    """Has the work moved since the last mark? A plan step finished, something new found, or
    several distinct actions that worked — read from the ledger and the trace, never asked of
    the work itself."""
    before, now = before or {}, now or {}
    return ((now.get("done") or 0) > (before.get("done") or 0)
            or (now.get("found") or 0) > (before.get("found") or 0)
            or (now.get("calls") or 0) >= (before.get("calls") or 0) + PROGRESS_CALLS)


def may_extend(job: dict[str, Any] | None, mark_now: dict[str, Any] | None) -> bool:
    attempts = (job or {}).get("attempts") or {}
    return ((attempts.get("extensions") or 0) < MAX_AUTO_EXTENSIONS
            and made_progress(attempts.get("mark"), mark_now))
