"""Turning a backlog of outcomes into individual lessons — one batched call.

A lesson is an OBSERVATION, never a behaviour change by itself. Nothing here can
alter how Jarvis behaves; that needs a proposal, which needs a pattern, which
needs corroboration. Keeping those steps separate is what makes "notice a
pattern, don't patch a one-off" real rather than aspirational.

**Outcomes are only marked reviewed when the call actually succeeded.** The
original discarded them whenever its one model call failed, which on a roster
that is routinely all rate-limited at once means silently losing real material.
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

MIN_OUTCOMES = 5
CADENCE_HOURS = 4
MAX_PER_BATCH = 40

SYSTEM = ("You review an assistant's own completed work and note what is worth "
          "learning from it. You are specific and conservative: a single failure is "
          "not a pattern, and you never invent a cause the record does not show.")


def _due(now: datetime | None = None) -> bool:
    last = store.get_last_run_at("reflect")
    if not last:
        return True
    try:
        previous = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
    except ValueError:
        return True
    now = now or datetime.now(timezone.utc)
    return (now - previous) >= timedelta(hours=CADENCE_HOURS)


def _prompt(outcomes: list[dict[str, Any]]) -> str:
    lines = []
    for outcome in outcomes:
        lines.append(
            f"- [{outcome['id']}] {outcome['source']}/{outcome.get('kind') or '?'}: "
            f"\"{outcome.get('title')}\" ended {outcome['status']}"
            + (f", error: {outcome['error']}" if outcome.get("error") else "")
            + (f", retries: {outcome['retries']}" if outcome.get("retries") else ""))
    return "\n".join([
        "Here is a batch of things this assistant recently did, and how each ended.",
        "Note anything genuinely worth learning — about how it works, not about the user.",
        "",
        *lines,
        "",
        'Reply with JSON: {"lessons": [{"text": "...", "scope": "general or tool:<name>", '
        '"evidence": ["outcome id", ...], "confidence": 0.0-1.0}]}.',
        "Every lesson must cite the outcome ids it came from. An empty list is a "
        "completely normal answer — most batches contain nothing worth keeping.",
        "Do not write a lesson about the user, their mood, or their relationships: "
        "this is only about the assistant's own working.",
    ])


def reflect(force: bool = False) -> dict[str, Any]:
    """One reflection cycle. Returns what it did, including why it did nothing."""
    outcomes = store.list_unreviewed_outcomes(MAX_PER_BATCH)
    if not force:
        if len(outcomes) < MIN_OUTCOMES:
            return {"ran": False, "reason": "not enough new material"}
        if not _due():
            return {"ran": False, "reason": "reflected recently"}
        if not store.try_consume_daily_budget():
            return {"ran": False, "reason": "today's budget for this is spent"}
    elif not outcomes:
        return {"ran": False, "reason": "nothing to reflect on"}

    try:
        answer = ask(_prompt(outcomes), system=SYSTEM, want_json=True,
                     task=Task(text="reflect on recent work", background=True,
                               needs_tools=False))
    except NoModelAvailable as err:
        # The outcomes stay unreviewed on purpose: losing real material because
        # the roster was rate-limited is exactly the failure this avoids.
        logger.info("reflection skipped — no model available: %s", err)
        return {"ran": False, "reason": "no model available", "keptForLater": len(outcomes)}

    data = answer.data if isinstance(answer.data, dict) else {}
    raw = data.get("lessons") if isinstance(data.get("lessons"), list) else []

    valid_ids = {o["id"] for o in outcomes}
    created = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text or is_excluded(text):
            continue
        # Evidence is filtered to ids that actually exist: a lesson citing an
        # outcome that never happened is worse than no lesson.
        evidence = [i for i in (item.get("evidence") or []) if i in valid_ids]
        if not evidence:
            continue
        confidence = item.get("confidence")
        created.append(store.create_lesson(
            text=text, scope=str(item.get("scope") or "general"), evidence=evidence,
            confidence=confidence if isinstance(confidence, (int, float)) else None))

    store.mark_outcomes_reviewed([o["id"] for o in outcomes])
    store.set_last_run_at("reflect")
    return {"ran": True, "reviewed": len(outcomes), "lessons": created}
