"""Backgrounding work, checking on it, and stopping it.

The live conversation is the only thing the user talks to. These three tools are
its whole interface to background work: start something, look in on it, stop it.
The worker's own reporting tools are separate and marked internal — they exist
for a job to report about itself, never for a conversation to call.

`work_in_background` is MEDIUM: it commits the assistant to acting while nobody
is watching, which is exactly the sort of thing to say out loud first. Checking
and stopping are LOW and MEDIUM respectively — looking costs nothing; stopping
throws away work in progress.
"""

from __future__ import annotations

from typing import Any

from ..capabilities import CapabilitySpec, Risk
from ..jobs import job_store
from ..jobs.orchestrator import AtCapacity, admit, cancel, resume

KINDS = ("generic", "research", "files", "computer")


def _start(goal: str = "", title: str = "", kind: str = "generic",
           priority: int = 2) -> dict[str, Any]:
    goal = (goal or "").strip()
    if not goal:
        return {"ok": False, "error": "There's nothing to work on."}
    if kind not in KINDS:
        kind = "generic"
    try:
        job = admit(title=(title or goal)[:80], goal=goal, kind=kind,
                    priority=max(1, min(3, int(priority or 2))))
    except AtCapacity as err:
        # Never a silent queue: name what is already running so the user can say
        # which should give way.
        running = ", ".join(f'"{j["title"]}"' for j in err.active) or "other work"
        return {"ok": False, "error": f"I'm already working on {running}. "
                                      "Which of those should I drop to take this on?"}
    if job["status"] == "awaiting_decision":
        return {"ok": True, "id": job["id"], "status": job["status"],
                "speak": f"{job['title']} would take over the computer — say the word and "
                         "I'll start."}
    return {"ok": True, "id": job["id"], "title": job["title"], "status": job["status"],
            "speak": f"Working on {job['title']} in the background."}


def _check(which: str | None = None, respond: str | None = None,
           guidance: str | None = None) -> dict[str, Any]:
    """Look in on background work, and — the one way a live conversation
    resolves a parked job — say to keep going."""
    job = _find(which) if which else None

    if respond == "keep_going":
        if job is None:
            return {"ok": False, "error": "I couldn't tell which job you meant."}
        resumed = resume(job["id"], guidance=guidance)
        return {"ok": True, "id": job["id"], "status": (resumed or {}).get("status"),
                "speak": f"Picking {job['title']} back up."}

    if job is not None:
        return {"ok": True, "job": _public(job)}
    active = job_store.list_active_jobs()
    if not active:
        recent = job_store.list_jobs(status="done", limit=3)
        return {"ok": True, "jobs": [], "recentlyFinished": [_public(j) for j in recent],
                "speak": "Nothing running right now."}
    return {"ok": True, "jobs": [_public(j) for j in active]}


def _stop(which: str = "") -> dict[str, Any]:
    job = _find(which)
    if job is None:
        return {"ok": False, "error": f'I couldn\'t find background work matching "{which}".'}
    cancel(job["id"])
    return {"ok": True, "stopped": job["title"], "speak": f"Stopped {job['title']}."}


def _stop_summary(args: dict[str, Any]) -> str:
    job = _find(str(args.get("which") or ""))
    if job is None:
        return f'I couldn\'t find background work matching "{args.get("which")}".'
    progress = f" It's about {job['progress']}% through." if job.get("progress") else ""
    return f'Stop working on "{job["title"]}"?{progress} Anything done so far is kept.'


def _start_summary(args: dict[str, Any]) -> str:
    goal = str(args.get("goal") or "").strip()
    return f'Work on this in the background: "{goal}"?'


def _find(which: str | None) -> dict[str, Any] | None:
    wanted = (which or "").strip().lower()
    if not wanted:
        active = job_store.list_active_jobs()
        # Exactly one thing running is unambiguous; more than one is not, and
        # guessing is how the wrong job gets cancelled.
        return active[0] if len(active) == 1 else None
    candidates = job_store.list_active_jobs() + job_store.list_jobs(limit=20)
    for job in candidates:
        if job["id"] == which or job["title"].lower() == wanted:
            return job
    return next((j for j in candidates if wanted in j["title"].lower()), None)


def _public(job: dict[str, Any]) -> dict[str, Any]:
    return {"id": job["id"], "title": job["title"], "status": job["status"],
            "kind": job["kind"], "priority": job["priority"],
            # Absent rather than 0 when the work has not reported one: 0% reads
            # as "stuck", which is a different thing from "hasn't said yet".
            **({"progress": job["progress"]} if job.get("progress") is not None else {}),
            **({"currentStep": job["currentStep"]} if job.get("currentStep") else {}),
            **({"result": job["result"]} if job.get("result") else {}),
            **({"error": job["error"]} if job.get("error") else {})}


SPECS = [
    CapabilitySpec(
        id="builtin.work_in_background", name="work_in_background",
        description=("Keep working on something in the background while the conversation "
                     "carries on. Use when the user asks you to keep at something, or when a "
                     "task will plainly take longer than a reply should."),
        input_schema={"type": "object", "properties": {
            "goal": {"type": "string", "description": "What to work on, in full."},
            "title": {"type": "string", "description": "A short name for it."},
            "kind": {"type": "string",
                     "description": '"generic", "research", "files", or "computer".'},
            "priority": {"type": "integer",
                         "description": "1 (most important) to 3. Default 2."}},
            "required": ["goal"]},
        risk=Risk.MEDIUM, handler=_start, summarize=_start_summary,
        timeout_s=20.0, tags=frozenset({"meta"}),
    ),
    CapabilitySpec(
        id="builtin.check_on_work", name="check_on_work",
        description=("Check on background work — what is running, how far along, or what "
                     "came of it. Also how you tell a job that is waiting on the user to "
                     "keep going, once they have said so."),
        input_schema={"type": "object", "properties": {
            "which": {"type": "string", "description": "Which job, in their own words."},
            "respond": {"type": "string", "description": 'Set to "keep_going" to resume a '
                                                         "job that is waiting on them."},
            "guidance": {"type": "string", "description": "Anything they said to do differently."}},
            "required": []},
        risk=Risk.LOW, handler=_check, timeout_s=15.0, tags=frozenset({"meta"}),
    ),
    CapabilitySpec(
        id="builtin.stop_working_on", name="stop_working_on",
        description="Stop a piece of background work.",
        input_schema={"type": "object", "properties": {
            "which": {"type": "string", "description": "Which job, in their own words."}},
            "required": ["which"]},
        risk=Risk.MEDIUM, handler=_stop, summarize=_stop_summary,
        timeout_s=15.0, tags=frozenset({"meta"}),
    ),
]
