"""Admission, supervision, and the one automatic retry.

The Conversation Manager (a live turn) decides WHETHER to background something
and does all the talking. This decides HOW the work actually gets done: whether
there is room for it, whether it needs a resource someone else holds, whether a
running job has stopped making progress, and whether a failure is worth one more
attempt or has to go to the user.

**Capacity is checked BEFORE anything is spent on planning**, so a full queue
does not cost a model call to discover. And a job is never silently queued past
capacity: the user is told and asked what should give way, because a queue nobody
can see is worse than a refusal they can answer.

**One automatic retry, total.** A crash retry and a stall retry share the same
counter, so a job cannot get two attempts by failing in two different ways.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from ..events import EventType, bus as default_bus
from ..events.bus import EventBus
from ..jscompat import now_iso
from . import job_store, worker
from .policy import (
    DIAGNOSE_TAIL_SIZE, can_auto_retry, classify_recovery, diagnose_stall, has_capacity,
    is_hung, resource_available,
)

logger = logging.getLogger(__name__)

#: How many jobs may be in flight at once. Small on purpose: these compete for
#: the same rate-limited model roster as the conversation the user is having.
MAX_ACTIVE_JOBS = 3

#: No heartbeat for this long on a running job means the process is gone.
HANG_TIMEOUT_MS = 5 * 60 * 1000

#: Kinds that hold something only one job may hold at a time.
RESOURCE_BY_KIND = {"computer": "computer"}


class AtCapacity(RuntimeError):
    def __init__(self, active: list[dict[str, Any]]):
        super().__init__("Already working on as much as I can at once.")
        self.active = active


def admit(*, title: str, goal: str, kind: str = "generic", priority: int = 2,
          conversation_id: str | None = None, parent_id: str | None = None,
          event_bus: EventBus | None = None) -> dict[str, Any]:
    """Create a job if there is room for it, and start it unless it must not start."""
    ebus = event_bus or default_bus
    active = job_store.list_active_jobs()
    if not has_capacity(active, MAX_ACTIVE_JOBS):
        raise AtCapacity(active)

    resource = RESOURCE_BY_KIND.get(kind)
    if not resource_available(active, resource):
        raise AtCapacity([j for j in active if j.get("resource") == resource])

    # A job that operates the real desktop never starts unattended: that is
    # exactly the outward-facing, hard-to-undo work that has to come back to the
    # owner first. It is parked awaiting a decision rather than queued, and the
    # ordinary "keep going" is the only thing that ever starts it.
    starts_parked = kind == "computer"
    job = job_store.create_job(
        title=title, goal=goal, kind=kind, priority=priority,
        conversation_id=conversation_id, parent_id=parent_id, resource=resource,
        status="awaiting_decision" if starts_parked else "queued")

    if starts_parked:
        job_store.add_outbox(tier=1, job_id=job["id"], reason="permission",
                             summary=f'"{title}" would take over the computer. OK to start?')
        ebus.publish(EventType.JOB_CREATED, {"id": job["id"], "status": job["status"]})
        return job

    ebus.publish(EventType.JOB_CREATED, {"id": job["id"], "title": title, "kind": kind})
    worker.run_in_background(job["id"], event_bus=ebus)
    return job


def resume(job_id: str, *, guidance: str | None = None,
           event_bus: EventBus | None = None) -> dict[str, Any] | None:
    """The one way a parked job starts again — used for every kind of park, so
    there is no separate machinery for "OK to start?" versus "OK to send it?"."""
    job = job_store.get_job(job_id)
    if job is None:
        return None
    if guidance:
        job_store.append_trace(job_id, phase="intent", effect="read", kind="note",
                               summary="guidance from the user", detail=guidance)
    job_store.update_job(job_id, {"status": "queued", "error": None})
    # Only the action that actually resolves the decision marks the row
    # delivered — showing it is not resolving it.
    job_store.deliver_all_for_job(job_id)
    worker.run_in_background(job_id, event_bus=event_bus)
    return job_store.get_job(job_id)


def cancel(job_id: str, event_bus: EventBus | None = None) -> dict[str, Any] | None:
    job = job_store.update_job(job_id, {"status": "cancelled", "finishedAt": now_iso(),
                                        "currentStep": None})
    job_store.deliver_all_for_job(job_id)
    (event_bus or default_bus).publish(EventType.JOB_UPDATED,
                                       {"id": job_id, "status": "cancelled"})
    return job


def supervise(now_ms: float | None = None, event_bus: EventBus | None = None) -> list[dict[str, Any]]:
    """One pass over everything in flight. Returns what it did, for the tests and
    for a real "what happened while I was away" answer."""
    ebus = event_bus or default_bus
    now_ms = now_ms if now_ms is not None else datetime.now(timezone.utc).timestamp() * 1000
    actions: list[dict[str, Any]] = []

    for job in job_store.list_active_jobs():
        if job["status"] == "running" and is_hung(job, now_ms=now_ms, timeout_ms=HANG_TIMEOUT_MS):
            actions.append(_recover(job, "no heartbeat — the work looks hung", ebus))
            continue
        if job["status"] == "running":
            stall = diagnose_stall(job_store.get_trace_tail(job["id"], DIAGNOSE_TAIL_SIZE))
            if stall is not None:
                actions.append(_recover(job, stall["detail"], ebus, cause=stall["cause"]))

    for job in job_store.list_jobs(status="stalled"):
        actions.append(_recover(job, job.get("error") or "it stopped making progress", ebus))
    return [a for a in actions if a]


def _recover(job: dict[str, Any], detail: str, ebus: EventBus,
             cause: str = "stalled") -> dict[str, Any]:
    """Spend the single automatic retry, or hand it to the user.

    The nudge goes into the SAME session, so nothing about pausing and resuming
    discards the context the work has already built up.
    """
    job_id = job["id"]
    if can_auto_retry(job):
        job_store.update_job(job_id, {"retries": 1, "status": "queued", "error": None})
        job_store.append_trace(job_id, phase="intent", effect="read", kind="note",
                               summary=f"retrying after {cause}", detail=detail)
        worker.run_in_background(job_id, event_bus=ebus)
        return {"job": job_id, "action": "retried", "cause": cause}

    job_store.update_job(job_id, {"status": "awaiting_decision",
                                  "error": detail, "currentStep": None})
    job_store.add_outbox(tier=1, job_id=job_id, reason="stuck",
                         summary=f'"{job["title"]}" is stuck: {detail}')
    ebus.publish(EventType.JOB_UPDATED, {"id": job_id, "status": "awaiting_decision",
                                         "error": detail})
    return {"job": job_id, "action": "escalated", "cause": cause}


def recover_orphans(event_bus: EventBus | None = None) -> list[dict[str, Any]]:
    """At startup: a job marked `running` with nothing running it crashed.

    No heuristic is needed to know that — the process is gone. What the trace
    decides is whether picking it back up is SAFE, and the verdict is read from
    the record rather than asked of the job.
    """
    ebus = event_bus or default_bus
    out: list[dict[str, Any]] = []
    for job in job_store.list_jobs(status="running"):
        verdict = classify_recovery(job, job_store.get_trace(job["id"]))
        job_store.update_job(job["id"], {"recovery": verdict})
        if verdict == "resumable":
            job_store.update_job(job["id"], {"status": "queued"})
            worker.run_in_background(job["id"], event_bus=ebus)
        elif verdict == "restartable":
            job_store.update_job(job["id"], {"status": "queued"})
            worker.run_in_background(job["id"], event_bus=ebus)
        else:
            job_store.update_job(job["id"], {"status": "awaiting_decision"})
            job_store.add_outbox(
                tier=1, job_id=job["id"], reason="crashed",
                summary=(f'"{job["title"]}" stopped part-way through something that '
                         "cannot safely be repeated. What would you like to do?"
                         if verdict == "unrecoverable"
                         else f'"{job["title"]}" is waiting on you.'))
        out.append({"job": job["id"], "recovery": verdict})
    return out
