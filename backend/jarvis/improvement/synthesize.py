"""Looking across lessons for something that genuinely recurred.

**A proposal needs at least two lessons backed by at least two DISTINCT
outcomes.** Two lessons drawn from the same single event are one event described
twice, and letting that become a permanent rule is precisely the "patched a
one-off" failure the whole chain exists to prevent.

**A proposal's source tier is the WORST tier among its supporting lessons**, not
1. Otherwise a lesson read from a web page rides into auto-apply alongside
genuinely observed evidence, which is the one thing the tier floor exists to
stop.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from ..gateway.client import NoModelAvailable, ask
from ..gateway.routing import Task
from . import store
from .domains import is_excluded

logger = logging.getLogger(__name__)

MIN_LESSONS = 2
MIN_DISTINCT_OUTCOMES = 2
CADENCE_HOURS = 24

SYSTEM = ("You look for patterns across an assistant's own noted lessons and propose "
          "a concrete change to how it works. You propose only what the evidence "
          "actually supports, and you say when there is no pattern.")


def _due(now: datetime | None = None) -> bool:
    last = store.get_last_run_at("synthesize")
    if not last:
        return True
    try:
        previous = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
    except ValueError:
        return True
    return ((now or datetime.now(timezone.utc)) - previous) >= timedelta(hours=CADENCE_HOURS)


def _prompt(lessons: list[dict[str, Any]]) -> str:
    lines = [f"- [{l['id']}] ({l['scope']}) {l['text']}" for l in lessons]
    return "\n".join([
        "Here are lessons this assistant has noted about its own work.",
        "Find anything that RECURS — the same problem showing up more than once — and "
        "propose one concrete change for it. Do not propose anything supported by only "
        "a single lesson.",
        "",
        *lines,
        "",
        'Reply with JSON: {"proposals": [{"title": "...", "kind": "rule" or "setting" '
        'or "skill" or "code", "rationale": "...", "text": "the rule, if kind is rule", '
        '"lessons": ["lesson id", ...]}]}.',
        'Use "rule" for a change to how it behaves, "setting" for a preference, '
        '"skill" or "code" for something needing real new work. An empty list is a '
        "normal answer.",
    ])


def synthesize(force: bool = False) -> dict[str, Any]:
    lessons = [l for l in store.list_lessons(status="active") if l.get("evidence")]
    if len(lessons) < MIN_LESSONS:
        return {"ran": False, "reason": "not enough lessons yet"}
    if not force:
        if not _due():
            return {"ran": False, "reason": "synthesised recently"}
        if not store.try_consume_daily_budget():
            return {"ran": False, "reason": "today's budget for this is spent"}

    try:
        answer = ask(_prompt(lessons), system=SYSTEM, want_json=True,
                     task=Task(text="look for patterns", background=True, needs_tools=False))
    except NoModelAvailable as err:
        logger.info("synthesis skipped — no model available: %s", err)
        return {"ran": False, "reason": "no model available"}

    by_id = {l["id"]: l for l in lessons}
    data = answer.data if isinstance(answer.data, dict) else {}
    raw = data.get("proposals") if isinstance(data.get("proposals"), list) else []

    created: list[dict[str, Any]] = []
    rejected: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        supporting = [by_id[i] for i in (item.get("lessons") or []) if i in by_id]
        if not title or is_excluded(title):
            continue

        if len(supporting) < MIN_LESSONS:
            rejected.append(f"{title}: only {len(supporting)} lesson(s) behind it")
            continue
        outcomes = {ref for lesson in supporting for ref in lesson.get("evidence") or []}
        if len(outcomes) < MIN_DISTINCT_OUTCOMES:
            # Two lessons from one event are one event described twice.
            rejected.append(f"{title}: all its lessons come from the same event")
            continue

        source_tier = max(int(l.get("source_tier") or 1) for l in supporting)
        created.append(store.create_proposal(
            kind=str(item.get("kind") or "rule"), title=title,
            rationale=str(item.get("rationale") or ""),
            payload={"text": item.get("text") or title},
            evidence=sorted(outcomes), source_tier=source_tier))

    store.set_last_run_at("synthesize")
    return {"ran": True, "proposals": created, "rejected": rejected}
