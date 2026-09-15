"""Background work that is waiting on the user.

Not job completions — those already have their own notification the moment they
happen. What this closes is the real gap: a job parked needing permission sits
completely silent until the user happens to start a conversation, which could be
hours after the thing it needed permission for stopped being useful.

**Its own dedup memory, and this is where it was learned.** A tier-3 verdict
creates no outbox row, so the broker's own duplicate check cannot see it, and a
job sitting in the same "still waiting" state produced a fresh finding, a fresh
model call and a fresh notification on every tick — forever, for one unanswered
question. The fix is to remember the specific outbox row last reported and stay
quiet until that actually changes.
"""

from __future__ import annotations

from typing import Any

from .registry import Source

SOURCE_ID = "jobs"
#: A cheap database read, so it can be frequent.
CHECK_INTERVAL_MS = 3 * 60 * 1000

WAITING_STATUSES = ("queued", "planning", "running", "awaiting_decision", "orphaned")


def list_items() -> list[dict[str, Any]]:
    """One item per job holding an undelivered tier-1 row.

    Keyed on the JOB, not the outbox row: a job holds one live ask at a time, and
    keying on the row would spawn a second schedule entry for each new ask.
    """
    from ...heartbeat import outbox
    from ...jobs import job_store

    items = []
    for job in job_store.list_active_jobs():
        if job.get("status") not in WAITING_STATUSES:
            continue
        waiting = [e for e in outbox.for_job(job["id"])
                   if e["tier"] == 1 and not e["deliveredAt"]]
        if waiting:
            items.append({"itemKey": job["id"], "intervalMs": CHECK_INTERVAL_MS})
    return items


def check(item_key: str) -> dict[str, Any]:
    from ...heartbeat import outbox, schedule_store
    from ...jobs import job_store

    job = job_store.get_job(item_key)
    if job is None:
        return {"finding": None}

    waiting = [e for e in outbox.for_job(item_key) if e["tier"] == 1 and not e["deliveredAt"]]
    if not waiting:
        # Nothing outstanding any more: forget which row was reported, so a
        # genuinely new ask is reported rather than suppressed.
        return {"finding": None, "checkState": None}

    entry = waiting[0]
    item = schedule_store.get_item(SOURCE_ID, item_key)
    reported = ((item or {}).get("checkState") or {}).get("lastReportedOutboxId")
    if reported == entry["id"]:
        return {"finding": None}         # the same unanswered ask

    return {
        "finding": {"summary": f'Background work is waiting on you: "{job["title"]}" — '
                               f'{entry["summary"]}',
                    "detail": {"jobId": item_key, "outboxId": entry["id"]}},
        "checkState": {"lastReportedOutboxId": entry["id"]},
    }


source = Source(id=SOURCE_ID, default_interval_ms=CHECK_INTERVAL_MS,
                list_items=list_items, check=check)
