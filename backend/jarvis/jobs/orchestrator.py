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
import os
import threading
from datetime import datetime, timezone
from typing import Any

from ..events import EventType, bus as default_bus
from ..events.bus import EventBus
from ..jscompat import now_iso
from . import job_store, worker
from .policy import (
    DIAGNOSE_TAIL_SIZE, can_auto_retry, classify_recovery, diagnose_stall, has_capacity,
    is_hung, open_external_intents, resource_available,
)

logger = logging.getLogger(__name__)

#: How often supervise() sweeps everything in flight. Frequent relative to
#: HANG_TIMEOUT_MS (below) so a genuinely hung job is not sitting undetected
#: for most of its own hang window.
TICK_SECONDS = 60.0
ENABLE_ENV = "JARVIS_JOBS"

_timer: threading.Timer | None = None


def is_enabled() -> bool:
    return os.environ.get(ENABLE_ENV) == "1"

#: How many jobs may be in flight at once, when the user has not said. Small on
#: purpose: these compete for the same models as the conversation the user is
#: having.
MAX_ACTIVE_JOBS = 3

#: No heartbeat for this long on a running job means the process is gone.
HANG_TIMEOUT_MS = 5 * 60 * 1000

#: Kinds that hold something only one job may hold at a time.
RESOURCE_BY_KIND = {"computer": "computer"}


def active_job_limit() -> int:
    """How many jobs may run at once, as the USER set it.

    `prefs.maxBackgroundJobs` existed and nothing read it, so lowering the limit
    changed nothing — the constant above won every time. One reader now, used by
    admission and by anything else that has to know whether there is room.
    """
    from ..prefs import get_prefs

    try:
        wanted = int(get_prefs().get("maxBackgroundJobs", MAX_ACTIVE_JOBS))
    except (TypeError, ValueError):
        return MAX_ACTIVE_JOBS
    return max(1, wanted)


class AtCapacity(RuntimeError):
    def __init__(self, active: list[dict[str, Any]]):
        super().__init__("Already working on as much as I can at once.")
        self.active = active


def admit(*, title: str, goal: str, kind: str = "generic", priority: int = 2,
          conversation_id: str | None = None, parent_id: str | None = None,
          agent_id: str | None = None,
          event_bus: EventBus | None = None) -> dict[str, Any]:
    """Create a job if there is room for it, and start it unless it must not start."""
    ebus = event_bus or default_bus
    active = job_store.list_active_jobs()
    if not has_capacity(active, active_job_limit()):
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
        status="awaiting_decision" if starts_parked else "queued", agent_id=agent_id)

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
    there is no separate machinery for "OK to start?" versus "OK to send it?".

    It continues from the last finished round, with everything the work had already
    learned; guidance, if given, is what the next round is told.
    """
    job = job_store.get_job(job_id)
    if job is None:
        return None
    if guidance:
        job_store.append_trace(job_id, phase="intent", effect="read", kind="nudge",
                               summary="guidance from the user", detail=guidance)
    job_store.update_job(job_id, {"status": "queued", "error": None})
    # Only the action that actually resolves the decision marks the row
    # delivered — showing it is not resolving it.
    job_store.deliver_all_for_job(job_id)
    worker.run_in_background(job_id, event_bus=event_bus,
                             answer={"guidance": guidance} if guidance else {})
    return job_store.get_job(job_id)


def restart(job_id: str, event_bus: EventBus | None = None) -> dict[str, Any] | None:
    """Start it over from the goal: its saved rounds are dropped, so everything it did is
    done again. Whether that is safe is the caller's question (`routes/jobs.py`)."""
    from .. import durable

    if job_store.get_job(job_id) is None:
        return None
    durable.stop(job_id)
    job_store.update_job(job_id, {"status": "queued", "result": None, "error": None,
                                  "progress": 0, "currentStep": None, "retries": 0})
    job_store.deliver_all_for_job(job_id)
    worker.run_in_background(job_id, event_bus=event_bus, fresh=True)
    return job_store.get_job(job_id)


def answer_approval(job_id: str, approval_id: str, *, allowed: bool, result: Any = None,
                    event_bus: EventBus | None = None) -> dict[str, Any] | None:
    """The person answered what a job was waiting on: settle it into the job's own working
    transcript and let the job carry on by itself. Nothing happens unless the job really is
    waiting on exactly that approval."""
    from .. import durable

    job = job_store.get_job(job_id)
    work = durable.peek(job_id) or {}
    waiting = work.get("waiting_on") or {}
    if job is None:
        return None
    if work.get("status") == "waiting":
        if (waiting.get("approval") or {}).get("id") != approval_id:
            return None
    elif not (work.get("status") == "running" and job.get("status") == "awaiting_decision"):
        # Running means it stopped just as it was parking (a crash); it reaches the wait
        # again and the answer applies there (`durable.advance`). Anything else: not waiting.
        return None
    job_store.update_job(job_id, {"status": "queued", "error": None})
    job_store.deliver_all_for_job(job_id)
    worker.run_in_background(job_id, event_bus=event_bus,
                             answer={"approval": {"allowed": allowed, "result": result}})
    return job_store.get_job(job_id)


def cancel(job_id: str, event_bus: EventBus | None = None) -> dict[str, Any] | None:
    from .. import durable

    job = job_store.update_job(job_id, {"status": "cancelled", "finishedAt": now_iso(),
                                        "currentStep": None})
    # The round in flight stops at its next step; no later round starts (`worker._stopped`).
    durable.stop(job_id)
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
            actions.append(_recover(job, "no heartbeat — the work looks hung", ebus, live=True))
            continue
        if job["status"] == "running":
            stall = diagnose_stall(job_store.get_trace_tail(job["id"], DIAGNOSE_TAIL_SIZE))
            if stall is not None:
                actions.append(_recover(job, stall["detail"], ebus, cause=stall["cause"],
                                        live=True))

    for job in job_store.list_jobs(status="stalled"):
        actions.append(_recover(job, job.get("error") or "it stopped making progress", ebus))
    return [a for a in actions if a]


def _recover(job: dict[str, Any], detail: str, ebus: EventBus,
             cause: str = "stalled", live: bool = False) -> dict[str, Any]:
    """Spend the single automatic retry, or hand it to the user.

    A job still running (hung, or going round in circles) is stopped first, so it never
    runs twice at once. The retry continues from its last finished round — nothing about
    pausing and resuming discards what the work has already done — and is told why.
    """
    from .. import durable

    job_id = job["id"]
    if live:
        durable.stop(job_id)
    if can_auto_retry(job):
        job_store.update_job(job_id, {"retries": 1, "status": "queued", "error": None})
        job_store.append_trace(job_id, phase="intent", effect="read", kind="nudge",
                               summary=f"retrying after {cause}", detail=detail)
        worker.run_in_background(job_id, event_bus=ebus, answer={"note": detail})
        return {"job": job_id, "action": "retried", "cause": cause}

    job_store.update_job(job_id, {"status": "awaiting_decision",
                                  "error": detail, "currentStep": None})
    summary = f'"{job["title"]}" is stuck: {detail}'
    entry = job_store.add_outbox(tier=1, job_id=job_id, reason="stuck", summary=summary)
    ebus.publish(EventType.JOB_UPDATED, {"id": job_id, "status": "awaiting_decision",
                                         "error": detail})
    worker._ask_aloud(job_id, entry, summary, "A background job is stuck.")
    return {"job": job_id, "action": "escalated", "cause": cause}


def recover_orphans(event_bus: EventBus | None = None) -> list[dict[str, Any]]:
    """At startup: a job marked `running` with nothing running it crashed.

    No heuristic is needed to know that — the process is gone. What the trace
    decides is whether picking it back up is SAFE, and the verdict is read from
    the record rather than asked of the job: an action that reaches outside Jarvis and
    started without a recorded outcome may or may not have happened, so the person is
    asked to check that one action. Anything else continues from its last finished
    round, and finished actions are never repeated (`jarvis/durable.py`).

    A job still `queued` never got its worker before the process stopped; it starts now.
    """
    ebus = event_bus or default_bus
    out: list[dict[str, Any]] = []
    for job in job_store.list_jobs(status="queued", limit=500):
        worker.run_in_background(job["id"], event_bus=ebus)
        out.append({"job": job["id"], "recovery": "started"})
    for job in job_store.list_jobs(status="running", limit=500):
        trace = job_store.get_trace(job["id"])
        verdict = classify_recovery(job, trace, recorded=_recorded_operations(trace))
        # The durable runner's own write-ahead record says the same from its side, for an
        # action whose trace row never got written.
        in_doubt = _durable_unsure(job["id"])
        if verdict == "resumable" and in_doubt:
            verdict = "unrecoverable"
        job_store.update_job(job["id"], {"recovery": verdict})
        # A crash is worth learning from in its own right — whatever the job
        # goes on to do next. A real, disclosed gap until now: this used to
        # publish no event and call nothing, so the crash itself never reached
        # Self-Improvement (see improvement/CLAUDE.md's "hook points").
        try:
            from ..observers.improvement import _record_job_crash

            _record_job_crash(job, trace, verdict)
        except Exception:  # noqa: BLE001 — capture must never block real recovery
            logger.exception("could not record the crash of job %s for Self-Improvement",
                             job["id"])
        if verdict == "resumable":
            job_store.update_job(job["id"], {"status": "queued"})
            worker.run_in_background(job["id"], event_bus=ebus)
        else:
            unsure = open_external_intents(trace, _recorded_operations(trace)) or \
                [{"name": a["capability"], "operationId": a["operationId"]} for a in in_doubt]
            name = (unsure[-1].get("name") if unsure else None) or "an action"
            job_store.update_job(job["id"], {"status": "awaiting_decision",
                                             "currentStep": f"check {name}"})
            crashed_summary = (
                f'"{job["title"]}" stopped in the middle of {name}, which reaches '
                "outside Jarvis, so I can't tell whether it happened. Please check, "
                "then tell me to keep going (it may try it again) or discard the job."
                if verdict == "unrecoverable" else f'"{job["title"]}" is waiting on you.')
            entry = job_store.add_outbox(tier=1, job_id=job["id"], reason="crashed",
                                         detail={"unsure": unsure}, summary=crashed_summary)
            worker._ask_aloud(job["id"], entry, crashed_summary, "A background job stopped.")
        out.append({"job": job["id"], "recovery": verdict})
    return out


def _durable_unsure(job_id: str) -> list[dict[str, Any]]:
    from .. import durable

    return durable.unsure(job_id) if durable.peek(job_id) is not None else []


def _recorded_operations(trace: list[dict[str, Any]]) -> set[str]:
    """Which of the trace's actions have a recorded result in the operations table — those
    finished even if the trace's own outcome row never got written."""
    from ..db import get_db

    ids = [d.get("operationId") for d in (_detail_of(r) for r in trace)
           if isinstance(d, dict) and d.get("operationId")]
    if not ids:
        return set()
    marks = ",".join("?" for _ in ids)
    rows = get_db().execute(f"SELECT operation_id FROM operations WHERE operation_id IN ({marks})",
                            ids).fetchall()
    return {r[0] for r in rows}


def _detail_of(row: dict[str, Any]) -> Any:
    import json

    raw = row.get("detail")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return None
    return raw


def start(*, event_bus: EventBus | None = None) -> bool:
    """Start periodic supervision, if the interlock allows it. Returns whether
    it started — same shape as scheduler.engine.start()/heartbeat.engine.start().

    Sweeps for orphaned jobs once, at startup, before the first tick — same
    "startup only" placement as heartbeat.engine.start()'s own stale-running
    reset, and for the same reason: a crash leaves state only a fresh process
    boot can honestly resolve.
    """
    global _timer
    if not is_enabled() or _timer is not None:
        return False

    # Specialist runs first: building the registry closes off runs nothing is running any
    # more, and that must happen before a recovered job starts a run of its own.
    try:
        from ..agents.durable_runs import recover_at_startup

        recover_at_startup()
    except Exception:  # noqa: BLE001
        logger.exception("specialist run recovery failed at startup")
    try:
        recover_orphans(event_bus=event_bus)
    except Exception:  # noqa: BLE001
        logger.exception("orphan recovery failed at startup")

    def run() -> None:
        global _timer
        try:
            supervise(event_bus=event_bus)
        except Exception:  # noqa: BLE001
            logger.exception("a supervision pass failed")
        _timer = threading.Timer(TICK_SECONDS, run)
        _timer.daemon = True
        _timer.start()

    run()
    return True


def stop() -> None:
    global _timer
    if _timer is not None:
        _timer.cancel()
        _timer = None
