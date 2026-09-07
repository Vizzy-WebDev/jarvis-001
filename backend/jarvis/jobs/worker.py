"""Running one job's work, on its own session.

**A worker structurally cannot write into the conversation the user is looking
at.** Its session id is `job:<id>`, sessions are keyed separately, and a worker
session is never bound to chat history — so its working turns never appear in the
conversation list and never share a transcript with what the user is actually
talking about. That is a property of the wiring, not a convention to remember.

**A worker never asks the user anything directly.** It runs with
`Autonomy.ESCALATE`, so a call needing a human parks into `awaiting_decision`
with an outbox row rather than minting a confirmation nobody is present to
answer. A job may wait hours; a short-lived token would be long expired.

Every effectful step is traced before and after, which is what makes recovery a
reading of the record rather than a guess.
"""

from __future__ import annotations

import logging
import threading
import uuid
from typing import Any

from ..events import EventType, bus as default_bus
from ..events.bus import EventBus
from ..jscompat import now_iso
from ..policy import Autonomy, Surface
from . import job_store
from .policy import DIAGNOSE_TAIL_SIZE, diagnose_stall, step_budget_exceeded

logger = logging.getLogger(__name__)

#: Tool sets per kind. `generic` is deliberately the FULL catalogue with no
#: fence — it is not "no expertise", it is the opposite. The named kinds are
#: small hardcoded lists chosen for work whose shape is known.
TOOLS_BY_KIND: dict[str, list[str] | None] = {
    "generic": None,
    "research": ["look_it_up", "read_web_page", "get_headlines", "search_conversations"],
    "files": ["read_web_page", "search_conversations"],
}


def session_for(job_id: str) -> str:
    return f"job:{job_id}"


def run_job(job_id: str, *, event_bus: EventBus | None = None,
            max_steps: int | None = None) -> dict[str, Any]:
    """Drive one job to a conclusion, or to the point where a person is needed."""
    from ..assembly import get_orchestrator
    from ..orchestrator import ApprovalRequired, Chunk, Done, Failed, ToolRan, TurnRequest

    ebus = event_bus or default_bus
    job = job_store.get_job(job_id)
    if job is None:
        raise KeyError(f"Unknown job: {job_id}")

    job_store.update_job(job_id, {"status": "running", "startedAt": job.get("startedAt") or now_iso()})
    job_store.heartbeat(job_id, step="starting")
    ebus.publish(EventType.JOB_UPDATED, {"id": job_id, "status": "running"})

    allowed = TOOLS_BY_KIND.get(job["kind"], None)
    request = TurnRequest(
        text=job["goal"],
        session_id=session_for(job_id),
        surface=Surface.JOB,
        # Nobody is present. A decision is parked, never asked.
        autonomy=Autonomy.ESCALATE,
        turn_id=uuid.uuid4().hex,
        allowed_names=frozenset(allowed) if allowed else None,
    )

    answer, parked, failure, steps = "", None, None, 0
    for event in get_orchestrator().run_turn(request):
        if isinstance(event, Chunk):
            continue
        if isinstance(event, ToolRan):
            steps += 1
            job_store.append_trace(
                job_id, phase="outcome", effect="workspace" if event.ok else "read",
                kind="tool", summary=f"{event.capability} {event.outcome.value}",
                detail={"name": event.capability, "ok": event.ok, "error": event.error})
            job_store.heartbeat(job_id, step=f"using {event.capability}")
            if max_steps and step_budget_exceeded(steps, job["kind"]):
                break
        elif isinstance(event, ApprovalRequired):
            parked = event
        elif isinstance(event, Done):
            answer = event.text
        elif isinstance(event, Failed):
            failure = event

    if parked is not None:
        return _park(job_id, parked, ebus)
    if failure is not None:
        return _stall(job_id, failure.error, ebus)

    stall = diagnose_stall(job_store.get_trace_tail(job_id, DIAGNOSE_TAIL_SIZE))
    if stall is not None and not answer:
        return _stall(job_id, stall["detail"], ebus, cause=stall["cause"])

    job_store.update_job(job_id, {"status": "done", "result": answer,
                                  "finishedAt": now_iso(), "progress": 100,
                                  "currentStep": None})
    job_store.append_trace(job_id, phase="outcome", effect="read", kind="note",
                           summary="finished", detail=answer[:500])
    ebus.publish(EventType.JOB_COMPLETED, {"id": job_id, "status": "done",
                                           "title": job["title"]})
    # Tier 3: worth recording, never worth interrupting for. The user finds it
    # when they ask, or through the ambient notification channel.
    job_store.add_outbox(tier=3, summary=f'"{job["title"]}" finished.', job_id=job_id,
                         reason="finished")
    return {"status": "done", "result": answer}


def _park(job_id: str, parked: Any, ebus: EventBus) -> dict[str, Any]:
    job = job_store.get_job(job_id) or {}
    job_store.update_job(job_id, {"status": "awaiting_decision",
                                  "currentStep": f"waiting on {parked.capability}"})
    job_store.append_trace(job_id, phase="intent", effect="external", kind="decision",
                           summary=f"{parked.capability} needs a decision",
                           detail={"approvalId": parked.approval_id})
    # Tier 1: the job cannot continue without an answer, so this is worth
    # raising the next time the user is here.
    job_store.add_outbox(tier=1, job_id=job_id, reason="permission",
                         summary=f'"{job.get("title")}" needs your go-ahead: {parked.reason}',
                         detail={"approvalId": parked.approval_id,
                                 "capability": parked.capability})
    ebus.publish(EventType.JOB_UPDATED, {"id": job_id, "status": "awaiting_decision"})
    return {"status": "awaiting_decision", "approvalId": parked.approval_id}


def _stall(job_id: str, detail: str, ebus: EventBus, cause: str = "failed") -> dict[str, Any]:
    """A job that got stuck. The orchestrator decides whether to spend its one
    retry — this only records what happened, so that decision is made from a
    record rather than from whatever the worker felt like reporting."""
    job_store.update_job(job_id, {"status": "stalled", "error": detail})
    job_store.append_trace(job_id, phase="outcome", effect="read", kind="note",
                           summary=f"stalled: {cause}", detail=detail)
    ebus.publish(EventType.JOB_UPDATED, {"id": job_id, "status": "stalled",
                                         "error": detail})
    return {"status": "stalled", "error": detail, "cause": cause}


#: Live worker threads. Tracked rather than fire-and-forgotten so shutdown can
#: wait for them: a worker still writing to the database while the process tears
#: it down is a segfault, not an exception — found exactly that way, by a test
#: closing the connection under a running job.
_threads: list[threading.Thread] = []
_threads_lock = threading.Lock()


def run_in_background(job_id: str, event_bus: EventBus | None = None) -> threading.Thread:
    def _go() -> None:
        try:
            run_job(job_id, event_bus=event_bus)
        except Exception:  # noqa: BLE001 — a worker crash must not take the app down
            logger.exception("job %s crashed", job_id)
            try:
                job_store.update_job(job_id, {"status": "stalled", "error": "The work crashed."})
            except Exception:  # noqa: BLE001 — the database may already be gone
                logger.warning("could not record the crash of job %s", job_id)

    thread = threading.Thread(target=_go, name=f"job-{job_id}", daemon=True)
    with _threads_lock:
        _threads[:] = [t for t in _threads if t.is_alive()]
        _threads.append(thread)
    thread.start()
    return thread


def join_all(timeout: float = 10.0) -> bool:
    """Wait for every running worker. Returns whether they all finished.

    Called at shutdown and between tests. A daemon thread is killed abruptly at
    process exit, which for one holding a database write is how a clean stop
    becomes a corrupt file.
    """
    with _threads_lock:
        alive = [t for t in _threads if t.is_alive()]
    for thread in alive:
        thread.join(timeout=timeout)
    return all(not t.is_alive() for t in alive)
