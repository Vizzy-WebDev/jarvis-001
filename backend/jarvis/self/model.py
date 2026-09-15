"""What Jarvis actually knows about itself, assembled from evidence.

**The one rule everything here answers to: every claim is grounded in a counter,
a row, live state, or a real policy value — never a plausible-sounding
estimate.** Below `MIN_ATTEMPTS_FOR_RATIO` a reliability question returns
`no_track_record` rather than a premature ratio. A self-model that narrates
fluently without this is worse than none: it is performed self-awareness, which
is exactly what this exists to avoid.

One builder per dimension, dispatched by `build(only=[...])`. Omitting `only`
builds NOTHING — there is no default "everything", because assembling every
dimension costs real reads and a caller that wanted one should not silently pay
for nine.

This reads Memory, Jobs and Self-Improvement; it owns none of them. And nothing
under this package imports the executor, the orchestrator or the gateway: a
strong self-assessment can make Jarvis sound more confident about a claim, and it
structurally cannot skip a confirmation or an approval. `tests/test_architecture.py`
asserts that rather than trusting it.
"""

from __future__ import annotations

from typing import Any, Callable

#: Below this many attempts there is no rate worth quoting, only a small sample
#: that would read as authoritative.
MIN_ATTEMPTS_FOR_RATIO = 5

NO_TRACK_RECORD = "no_track_record"


def reliability_of(axis: str, key: str) -> dict[str, Any]:
    from . import store

    stat = store.get_stat(axis, key)
    attempts = int((stat or {}).get("attempts") or 0)
    failures = int((stat or {}).get("failures") or 0)
    if attempts < MIN_ATTEMPTS_FOR_RATIO:
        return {"key": key, "verdict": NO_TRACK_RECORD, "attempts": attempts,
                "failures": failures,
                "note": (f"Only {attempts} use(s) on record — not enough to say how "
                         "reliable this is.")}
    return {"key": key, "verdict": "measured", "attempts": attempts, "failures": failures,
            "successRate": round((attempts - failures) / attempts, 3)}


def _can_do(about: list[str] | None = None) -> dict[str, Any]:
    """What Jarvis can actually do reliably — with the recorder's own health,
    so "never used" and "the recorder is broken" are distinguishable."""
    from . import store

    keys = about or [s["key"] for s in store.list_stats(axis="tool")][:12]
    return {
        "capabilities": [reliability_of("tool", key) for key in keys],
        "recorder": store.capture_health_summary(),
    }


def _failure_modes() -> dict[str, Any]:
    """How Jarvis specifically tends to fail — a read-only view over the lessons
    Self-Improvement already keeps. This owns none of that data."""
    from ..improvement import store as improvement_store

    lessons = improvement_store.list_lessons(status="active")
    return {"lessons": [{"text": l["text"], "scope": l["scope"],
                         "evidenceCount": len(l.get("evidence") or [])}
                        for l in lessons[:10]]}


def _how_it_behaves() -> dict[str, Any]:
    from ..improvement import store as improvement_store

    rules = improvement_store.list_rules(active_only=True)
    return {"rules": [{"text": r["text"], "scope": r["scope"]} for r in rules]}


def _doing_now(session_id: str | None = None) -> dict[str, Any]:
    from ..jobs import job_store

    active = job_store.list_active_jobs()
    goal = None
    if session_id:
        from . import store

        goal = store.get_active_goal("conversation", session_id)
    return {
        "backgroundWork": [{"title": j["title"], "status": j["status"],
                            **({"progress": j["progress"]} if j.get("progress") is not None
                               else {}),
                            **({"currentStep": j["currentStep"]} if j.get("currentStep")
                               else {})}
                           for j in active],
        "declaredGoal": ({"goal": goal["goal_text"],
                          "theirWordsAtTheTime": goal["source_turn_text"],
                          "note": ("This is what I recorded I understood the goal to be, "
                                   "not a verified account of what they meant. Judge "
                                   "freshly whether it still serves what they asked.")}
                         if goal else None),
    }


def _whats_its_call() -> dict[str, Any]:
    """What is genuinely Jarvis's own decision. The numbers are read live from
    the policy modules and the sentences are BUILT from them, so the prose and
    the value cannot drift apart — they are one read, not two that agree."""
    from ..improvement.policy import MIN_EVIDENCE_BY_TRUST
    from ..memory.policy import THRESHOLDS
    from ..prefs import get_prefs

    prefs = get_prefs()
    memory_trust = str(prefs.get("memoryTrust") or "ask")
    improvement_trust = str(prefs.get("improvementTrust") or "ask")
    memory_threshold = THRESHOLDS.get(memory_trust, THRESHOLDS["ask"])
    evidence_needed = MIN_EVIDENCE_BY_TRUST.get(improvement_trust,
                                                MIN_EVIDENCE_BY_TRUST["ask"])

    return {
        "hardFloors": [
            ("Nothing about the user is saved without asking, at any setting."
             if memory_threshold == float("inf") else
             f"A fact about the user saves itself only above {memory_threshold} "
             "confidence; anything less is put to them."),
            ("No change to how I work applies itself, at any setting."
             if evidence_needed == float("inf") else
             f"A change to how I work applies itself only with at least "
             f"{int(evidence_needed)} separate pieces of evidence behind it."),
            "Anything that conflicts with something already saved always goes to them.",
            "Only what I have observed myself can apply itself; anything I read asks first.",
            "A high-risk action is always confirmed, including inside a scheduled task.",
        ],
        "settings": {"memoryTrust": memory_trust, "improvementTrust": improvement_trust},
    }


BUILDERS: dict[str, Callable[..., Any]] = {
    "can_do": _can_do,
    "failure_modes": _failure_modes,
    "how_it_behaves": _how_it_behaves,
    "doing_now": _doing_now,
    "whats_its_call": _whats_its_call,
}


def build(only: list[str] | None = None, *, about: list[str] | None = None,
          session_id: str | None = None) -> dict[str, Any]:
    """Assemble the named dimensions. No `only` builds nothing, on purpose."""
    out: dict[str, Any] = {}
    for name in only or []:
        builder = BUILDERS.get(name)
        if builder is None:
            continue
        if name == "can_do":
            out[name] = builder(about)
        elif name == "doing_now":
            out[name] = builder(session_id)
        else:
            out[name] = builder()
    return out
