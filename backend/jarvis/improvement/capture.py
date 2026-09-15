"""Turning things that already happened into outcome rows. Zero model calls.

This fires on every job and task completion and on every corrected turn — far
more often than the budget-gated reflection that later reads what it wrote — so
costing quota here would make the whole subsystem unaffordable. It is also why
this is safe to call from anywhere, including the hot path of a live turn.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from . import store

TERMINAL_JOB_STATUSES = frozenset({"done", "stalled", "cancelled", "failed"})

#: Narrow and word-matching, the same discipline as the tone floors: a floor
#: under the model's own record_lesson call, not a replacement for it. A false
#: positive files one low-signal outcome, which still needs corroboration before
#: it can become anything, so over-matching is cheap and under-matching is not.
CORRECTION_PATTERNS = [
    re.compile(p, re.I) for p in (
        r"\bno,?\s+that'?s not what i (asked|meant|wanted|said)\b",
        r"\bdon'?t do that again\b",
        r"\byou keep (doing|making|getting) (that|this|the same)( mistake| thing)?\s*(wrong)?\b",
        r"\bthat'?s not (right|correct|it)\b",
        r"\bthat'?s (wrong|incorrect)\b",
        r"\bnot what i (asked|meant|wanted|said)\b",
        r"\bplease stop (doing|saying) that\b",
        r"\bi (already |just )?told you (not to|to)\b",
        r"\bactually,?\s+i (meant|wanted|asked for)\b",
        r"\bnext time,?\s+(please\s+|just\s+|remember to\s+|make sure (?:you |to )?|don'?t\s+)",
    )
]


def looks_like_correction(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in CORRECTION_PATTERNS)


def record_job_outcome(job: dict[str, Any], trace: list[dict[str, Any]] | None = None,
                       escalations: int = 0) -> dict[str, Any] | None:
    """A job reached a terminal status. Idempotent on the job id, because both
    the supervisor and the worker have real paths that can report the same
    terminal status — and that must never count as two pieces of evidence."""
    if job.get("status") not in TERMINAL_JOB_STATUSES:
        return None

    trace = trace or []
    tool_rows = [t for t in trace if t.get("kind") == "tool" and t.get("phase") == "outcome"]
    failed = 0
    for row in tool_rows:
        try:
            import json

            if json.loads(row.get("detail") or "{}").get("ok") is False:
                failed += 1
        except (TypeError, ValueError):
            continue

    return store.record_outcome(
        source="job", source_ref=job["id"], entity_ref=job["id"],
        title=job.get("title") or "Background work", goal=job.get("goal"),
        kind=job.get("kind"), status=job["status"], retries=job.get("retries") or 0,
        error=job.get("error"),
        tool_summary={"totalToolCalls": len(tool_rows), "failedToolCalls": failed},
        escalations=escalations)


def record_job_crash(job: dict[str, Any], trace: list[dict[str, Any]] | None = None,
                     verdict: str = "unrecoverable") -> dict[str, Any] | None:
    """A job was found `running` with nothing actually running it — the process
    that owned it is gone. A real, disclosed gap until now (see
    `jobs/CLAUDE.md`'s "Crash recovery" and `improvement/CLAUDE.md`'s own
    "hook points" entry): `jobs/orchestrator.py`'s `recover_orphans()` moved the
    job to `queued`/`awaiting_decision` and published no event, so nothing about
    the crash itself ever reached Self-Improvement, whatever became of the job
    afterward.

    Deliberately its own `source` (`"job_crash"`, never `"job"`) with a fresh,
    never-repeating `source_ref` — a crash is a genuinely new event each time it
    happens, not a repeat of one seen before, so it must never collide with
    `record_job_outcome()`'s own per-job dedup key. Without that separation, a
    job that crashes once and later finishes normally (or crashes twice) would
    have one of those two facts silently dropped as a "duplicate" of the other.
    `entity_ref` stays the job's own id, so the two rows are still visibly about
    the same job to anything that groups on it later.
    """
    trace = trace or []
    return store.record_outcome(
        source="job_crash", source_ref=f"{job['id']}:{uuid.uuid4().hex[:8]}",
        entity_ref=job["id"], title=job.get("title") or "Background work",
        goal=job.get("goal"), kind=job.get("kind"), status=f"crashed:{verdict}",
        retries=job.get("retries") or 0, error=job.get("error"))


def record_notable_tool_outcome(name: str, *, reason: str | None = None,
                                escalated: bool = False) -> dict[str, Any] | None:
    """A single tool call worth Self-Improvement seeing on its own, separate
    from the rolling reliability tally `observers/recording.py` already keeps.

    Ported from the Node build's `self-capture.js` — the half of it that did
    NOT survive the S6 cutover (see `self/CLAUDE.md`'s "No separate
    self-capture.py" entry). There, `recordToolOutcome()` bumped the tally for
    every call but wrote one of these only for a NOTABLE outcome: a real
    failure, a call refused because it wasn't in this turn's allowed tools, or
    a confirmation that had to be parked rather than asked inline. All three
    reach here today: a real failure via `TOOL_FAILED`, the other two via the
    dedicated `TOOL_REFUSED`/`TOOL_ESCALATED` events `capabilities/execute.py`
    now publishes (neither call ever reaches `_run()`, so neither could ever
    have raised `TOOL_COMPLETED`/`TOOL_FAILED` in the first place).

    No `source_ref`: like a correction, each notable call is a new event, never
    a repeat of an earlier one. `entity_ref` is the tool's own name — the
    stable key `reflect.py`/`synthesize.py` group "this tool tends to fail this
    way" under, the same role a job's id or a task's saved id plays for those
    two sources.
    """
    return store.record_outcome(
        source="turn", entity_ref=name, title=f"Tool call: {name}", kind="tool",
        status="failed", error=reason, escalations=1 if escalated else 0)


def record_task_outcome(run: dict[str, Any], task: dict[str, Any] | None = None,
                        ) -> dict[str, Any] | None:
    """A scheduled task ran. `source_ref` is this ONE run, which is what dedupe
    keys on; `entity_ref` is the saved task, which is stable across every future
    run and is what a recurring task's own pattern groups under."""
    if not run.get("id"):
        return None
    action = (task or {}).get("action") or {}
    return store.record_outcome(
        source="task", source_ref=run["id"], entity_ref=(task or {}).get("id"),
        title=(task or {}).get("title") or run.get("title") or "Scheduled task",
        goal=action.get("text") or (task or {}).get("title"),
        kind=action.get("type"), status="done" if run.get("ok") else "failed",
        error=run.get("error"))


def note_correction(text: str) -> dict[str, Any] | None:
    """The user said something that reads as a correction.

    No dedupe key: unlike a terminal status, each correction is a genuinely new
    event and never a repeat of an earlier one.
    """
    if not looks_like_correction(text or ""):
        return None
    return store.record_outcome(
        source="correction", title="User correction", goal=(text or "")[:200],
        kind="conversation",
        # A correction IS the signal that something went wrong.
        status="failed")


def record_explicit_teaching(text: str) -> dict[str, Any] | None:
    """The user plainly taught something — not a correction, not an inference.

    Filed as an outcome purely so the lesson it produces has something real to
    cite: every lesson's evidence is meant to trace back to something that
    actually happened, and being told directly is exactly that.
    """
    body = (text or "").strip()
    if not body:
        return None
    return store.record_outcome(source="explicit",
                                title="User taught Jarvis something directly",
                                goal=body[:200], kind="conversation", status="noted")
