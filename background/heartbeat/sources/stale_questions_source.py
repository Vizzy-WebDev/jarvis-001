"""Questions Jarvis asked that nobody answered, cleared instead of piling up.

A go-ahead asked for in a conversation is only worth anything while that moment lasts. Left
alone it used to sit `pending` for ever — holding its arguments, counted as open, and never
mentioned again (the owner's install had 14 of them, the oldest three weeks old; the sweep that
times them out existed and nothing called it). After `STALE_AFTER` such a question is timed
out, a specialist run that stopped on it is closed off honestly, and the person is told ONCE,
in one notification, what was dropped — so "ask me again if you still want it" is something
they were actually told.

**Never the questions background work is waiting on** (a job's, a scheduled run's): that work
cannot go on without the answer, so those are raised again (`jobs_source`), not dropped.

No finding is returned: deciding how urgent "some old questions were dropped" is does not need
a model call, and it is never worth interrupting anyone for.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .registry import Source

logger = logging.getLogger(__name__)

SOURCE_ID = "stale_questions"
ITEM_KEY = "sweep"
CHECK_INTERVAL_MS = 60 * 60 * 1000
#: A question unanswered this long belongs to a moment that has passed.
STALE_AFTER = timedelta(hours=24)
#: Sessions whose own work is waiting on the answer.
WAITING_WORK = ("job:", "task:")


def list_items() -> list[dict[str, Any]]:
    return [{"itemKey": ITEM_KEY, "intervalMs": CHECK_INTERVAL_MS}]


def sweep(now: datetime | None = None, event_bus: Any = None) -> list[Any]:
    """Time out what is stale and say so once. Returns the approvals it timed out."""
    from ...jscompat import to_iso_z
    from ...policy import approvals

    cutoff = (now or datetime.now(timezone.utc)) - STALE_AFTER
    dropped = approvals.expire_older_than(to_iso_z(cutoff), event_bus,
                                          keep_session_prefixes=WAITING_WORK)
    if not dropped:
        return []
    _close_runs_waiting_on({a.id for a in dropped})
    names = sorted({a.capability for a in dropped})
    count = len(dropped)
    try:
        from ... import notifications

        notifications.add(
            kind="system", level="info",
            title=(f"I dropped {count} old questions you never answered" if count > 1
                   else "I dropped an old question you never answered"),
            body=("I asked for your go-ahead more than a day ago and never got one, so nothing "
                  f"was done ({', '.join(names)}). Ask me again if you still want any of it."),
            meta={"approvalIds": [a.id for a in dropped]}, event_bus=event_bus)
    except Exception:  # noqa: BLE001 — the questions are timed out either way
        logger.exception("could not say which old questions were dropped")
    return dropped


def _close_runs_waiting_on(approval_ids: set[str]) -> None:
    """A specialist run that stopped on one of these is not still waiting: it stopped."""
    try:
        from ...agents import store as agent_store

        for run in agent_store.list_runs(limit=500):
            if run["status"] == "awaiting_approval" and run.get("approvalId") in approval_ids:
                agent_store.finish_run(
                    run["id"], status="failed", result=run.get("result"),
                    error=("It needed your go-ahead and waited more than a day without one, so "
                           "it stopped. Ask again to pick it up."),
                    tools_used=run.get("toolsUsed"))
    except Exception:  # noqa: BLE001
        logger.exception("could not close the specialist runs that were waiting")


def check(item_key: str) -> dict[str, Any]:
    sweep()
    return {"finding": None}


source = Source(id=SOURCE_ID, default_interval_ms=CHECK_INTERVAL_MS,
                list_items=list_items, check=check)
