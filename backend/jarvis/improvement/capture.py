"""Turning things that already happened into outcome rows. Zero model calls.

This fires on every job and task completion and on every corrected turn — far
more often than the budget-gated reflection that later reads what it wrote — so
costing quota here would make the whole subsystem unaffordable. It is also why
this is safe to call from anywhere, including the hot path of a live turn.
"""

from __future__ import annotations

import re
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
