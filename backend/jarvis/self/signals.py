"""When is the self-model worth consulting? Pure, deterministic, zero imports.

This decides WHEN, never WHAT. It takes only plain data a caller has already
gathered, so it never needs to reach into any store and is leaf-safe by
construction rather than by discipline.

**The push path has a real limitation worth knowing before extending it:**
`known_failure` and `no_track_record` can only ever match something THIS turn has
already used in an earlier step. Nothing here has foreknowledge of what the model
is about to decide to call. Catching it before first use is the PULL path's job —
the model asking `check_myself` before making a claim about itself.
"""

from __future__ import annotations

from typing import Any

ACTIVE_JOB_STATUSES = frozenset({"queued", "running"})


def detect(*, tool_calls_this_turn: list[dict[str, Any]] | None = None,
           active_jobs: list[dict[str, Any]] | None = None,
           pending_memory_conflict: bool = False,
           correction_detected: bool = False,
           scopes_in_play: list[str] | None = None,
           active_lesson_scopes: list[str] | None = None,
           no_track_record_checks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    tool_calls = tool_calls_this_turn or []
    jobs = active_jobs or []
    scopes = scopes_in_play or []
    lesson_scopes = set(active_lesson_scopes or [])
    checks = no_track_record_checks or []

    authority = (any(call.get("needsConfirmation") for call in tool_calls)
                 or any(job.get("status") == "awaiting_decision" for job in jobs)
                 or bool(pending_memory_conflict))

    matched = [scope for scope in scopes if scope in lesson_scopes]
    missing = [{"axis": c.get("axis"), "key": c.get("key")}
               for c in checks if not c.get("attempts")]

    return {
        "authority": authority,
        "knownFailure": bool(matched),
        "matchedScopes": matched,
        "noTrackRecord": bool(missing),
        "noTrackRecordKeys": missing,
        "correction": bool(correction_detected),
        "blockedOnBackground": any(job.get("status") in ACTIVE_JOB_STATUSES for job in jobs),
    }


def any_fired(signals: dict[str, Any] | None) -> bool:
    """The single gate before spending any prompt space on this.

    A clean track record never fires it: only genuine prior trouble does.
    """
    if not signals:
        return False
    reliability = signals.get("toolReliability")
    notable = (isinstance(reliability, list)
               and any((r or {}).get("failures", 0) > 0 for r in reliability))
    return bool(signals.get("authority") or signals.get("knownFailure")
                or signals.get("noTrackRecord") or signals.get("correction")
                or signals.get("blockedOnBackground") or notable)
