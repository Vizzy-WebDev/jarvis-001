"""Background jobs over HTTP.

Long-running work happening while the conversation is about something else — a
different mechanism from a scheduled task, which runs on a clock nobody judged.
A job is work somebody, or Jarvis, decided to put in the background right now.

A surface over `jobs/`, which holds every rule about what a job is. Two of those
rules are visible here and neither is re-decided in this file:

**Capacity is checked before anything is spent.** Admission raises rather than
creating a job nobody has room to run, and this answers that with a plain 409 —
"there is no room right now" is a real state, not a failure.

**A job that operates the real desktop never starts unattended.** It is created
already parked, and `resume` — the ordinary "keep going" every other parked
decision uses — is the only thing that ever starts it. There is no separate
confirm mechanism, and adding one here would be a second answer.

**Restarting is not the same as resuming**, and the difference is the trace's to
decide, not this route's: whether picking a job back up is safe at all is read
from what it actually did, never asked of the job.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from ..heartbeat import outbox
from ..jobs import job_store, orchestrator

router = APIRouter(prefix="/api")


def _readable(row: dict[str, Any]) -> dict[str, Any]:
    """A trace row with its detail decoded.

    The trace writer JSON-encodes `detail` and the reader does not decode it, so
    a plain string comes back still wearing its quotes. Decoding is done HERE
    rather than in the reader because two existing consumers decode it
    themselves, and changing the reader under them would be a wider change than
    this needs — but a screen showing `"try the archive"` with the quotes visible
    is wrong, and it is this route's output that a screen reads.
    """
    detail = row.get("detail")
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except ValueError:
            pass  # not JSON after all: show it as it is rather than hiding it
    return {**row, "detail": detail}


@router.get("/jobs")
def listed(status: str | None = None) -> dict[str, Any]:
    """Everything, or one status. Comma-separated for several at once, which is
    what the screen's own filter sends."""
    wanted = [part.strip() for part in (status or "").split(",") if part.strip()]
    if not wanted:
        return {"jobs": job_store.list_jobs()}
    jobs = [job for state in wanted for job in job_store.list_jobs(status=state)]
    jobs.sort(key=lambda job: job.get("createdAt") or "", reverse=True)
    return {"jobs": jobs}


@router.post("/jobs")
def create(body: dict[str, Any] = Body(default_factory=dict)):
    goal = str(body.get("goal") or "").strip()
    if not goal:
        return JSONResponse({"ok": False, "error": "A job needs a goal."}, status_code=400)
    try:
        job = orchestrator.admit(
            title=str(body.get("title") or goal)[:120], goal=goal,
            kind=str(body.get("kind") or "generic"),
            conversation_id=body.get("conversationId"))
    except orchestrator.AtCapacity as full:
        # Not an error: everything is working, there is simply no room. 409 says
        # exactly that, and the screen can say which ones are in the way.
        return JSONResponse(
            {"ok": False, "error": "Too much is already running.",
             "active": [{"id": j["id"], "title": j["title"]} for j in (full.active or [])]},
            status_code=409)
    return {"ok": True, "job": job}


@router.get("/jobs/{job_id}")
def one(job_id: str):
    """The job beside what it actually DID, in one request.

    The trace is the honest part of this screen: an intent row is written before
    an action runs and an outcome row after it, so a crash between the two still
    leaves the intent on record. Reading it is how "it says it did this" can be
    told apart from "it did this".
    """
    job = job_store.get_job(job_id)
    if job is None:
        return JSONResponse({"ok": False, "error": "Unknown job."}, status_code=404)
    return {"job": job, "trace": [_readable(row) for row in job_store.get_trace(job_id)],
            "outbox": outbox.for_job(job_id)}


@router.post("/jobs/{job_id}/resume")
def resume(job_id: str, body: dict[str, Any] = Body(default_factory=dict)):
    """Keep going — the one mechanism every parked decision uses.

    Optional guidance goes into the SAME live session the job already has, so
    nothing about being parked and picked back up discards its context.
    """
    job = orchestrator.resume(job_id, guidance=(body.get("guidance") or None))
    if job is None:
        return JSONResponse({"ok": False, "error": "Unknown job."}, status_code=404)
    return {"ok": True, "job": job}


@router.post("/jobs/{job_id}/restart")
def restart(job_id: str, body: dict[str, Any] = Body(default_factory=dict)):
    """Start it over rather than continue it.

    Refused for a job the trace says is unrecoverable, and that refusal is the
    point: ANY row recording a real outward effect makes restarting unsafe,
    because repeating something that reached the outside world is not something
    a retry can take back. `force` exists for the person who knows better than
    the record, and has to say so explicitly.
    """
    job = job_store.get_job(job_id)
    if job is None:
        return JSONResponse({"ok": False, "error": "Unknown job."}, status_code=404)
    if job.get("recovery") == "unrecoverable" and not body.get("force"):
        return {"ok": False, "reason": "unrecoverable",
                "message": "This job already did something outside Jarvis that starting "
                           "over could repeat. Restart anyway?"}
    job_store.update_job(job_id, {"result": None, "error": None, "progress": 0,
                                  "currentStep": None})
    restarted = orchestrator.resume(job_id)
    return {"ok": True, "job": restarted}


@router.post("/jobs/{job_id}/discard")
def discard(job_id: str):
    job = orchestrator.cancel(job_id)
    if job is None:
        return JSONResponse({"ok": False, "error": "Unknown job."}, status_code=404)
    return {"ok": True, "job": job}
