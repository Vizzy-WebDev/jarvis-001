"""Has anything actually gone wrong with Jarvis itself lately?

Answered from real diagnostic runs and their traced outcomes — not from an
impression, and not from the absence of a memory of something going wrong. "I
think I'm fine" is exactly the answer this replaces.
"""

from __future__ import annotations

from typing import Any

from ..capabilities import CapabilitySpec, Risk


def _run(hours: int = 24) -> dict[str, Any]:
    from ..ops.diagnostics import checks, source

    checks.register_all()
    window = max(1, min(int(hours or 24), 24 * 14))
    health = source.recent_health(hours=window)

    failing = health["currentlyFailing"]
    if not health["recent"]:
        note = ("No checks have run yet, so there is nothing to report — say that, rather "
                "than saying everything is fine.")
    elif failing:
        note = "These are real, current failures, not a general impression."
    else:
        note = "Every check that has run recently passed."

    return {"ok": True, "hours": window, "currentlyFailing": failing,
            "checks": health["checksRegistered"], "recent": health["recent"][:20],
            "note": note}


SPEC = CapabilitySpec(
    id="builtin.check_my_health",
    name="check_my_health",
    description=("Check whether anything has actually gone wrong with Jarvis itself recently — "
                 "its database, memory, background work, scheduling, voice or security checks. "
                 "Use it whenever asked if you are working properly, or if something seems off "
                 "about your own behaviour. If nothing has run yet, say that rather than "
                 "claiming everything is fine."),
    input_schema={"type": "object", "properties": {
        "hours": {"type": "integer", "description": "How far back to look. Defaults to 24."}},
        "required": []},
    risk=Risk.LOW,
    handler=_run,
    timeout_s=15.0,
    tags=frozenset({"core", "meta"}),
)
