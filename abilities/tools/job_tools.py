"""Backgrounding work, checking on it, and stopping it.

The live conversation is the only thing the user talks to. These three tools are
its whole interface to background work: start something, look in on it, stop it.
The worker's own reporting tools are separate and marked internal — they exist
for a job to report about itself, never for a conversation to call.

`work_in_background` is MEDIUM because it commits the assistant to acting while nobody
is watching — but it declares `request_suffices`: the person asking for it IS the
go-ahead, so no card stands between "I'll keep at it" and the work actually starting (a
promise to follow up is only real if the work started). Checking and stopping are LOW and
MEDIUM respectively — looking costs nothing; stopping keeps whatever was done so far.

**It is how Jarvis takes ownership of something, so it is always in front of it** (`core`).
It used to be reachable only through `find_capability`, while the prompt said a promise was
only real once it had been called: across 41 real conversations it was called zero times.
It is also the `handoff`: on the last step of a reply the turn loop offers this one tool, so
work that did not fit in a reply is carried on instead of abandoned half-done.

**What it starts with is a brief, not a sentence** (`jobs/objective.py`): the outcome, what
done looks like, what the conversation settled — and, added here by code rather than left to
a paraphrase, the person's own recent words and what this reply had already found.
"""

from __future__ import annotations

import json
from typing import Any

from ..capabilities import CapabilitySpec, Risk
from ..jobs import job_store, objective
from ..jobs.orchestrator import AtCapacity, admit, cancel, resume

KINDS = ("generic", "research", "files", "computer")

#: Sessions that are someone's own working turn, not a conversation the person is in.
_WORKING_SESSIONS = ("job:", "task:", "agent:")
RESULT_CHARS = 500


def _conversation_of(ctx: Any) -> str | None:
    session = str(getattr(ctx, "session_id", "") or "")
    return session if session and not session.startswith(_WORKING_SESSIONS) else None


def _said_and_done(session_id: str | None) -> tuple[list[str], str]:
    """The person's own recent words in this conversation, and what the reply in progress has
    already run and found — read from the conversation itself, so the work that carries on
    starts from the record and not from a model's summary of it."""
    if not session_id:
        return [], ""
    from .. import conversation

    try:
        messages = list(conversation.get_messages(session_id))
    except Exception:  # noqa: BLE001 — the brief is still made without it
        return [], ""
    said = [str(m.get("text") or "").strip() for m in messages
            if m.get("role") == "user" and str(m.get("text") or "").strip()][-3:]
    last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    calls: dict[str, str] = {}
    lines: list[str] = []
    for message in messages[last_user + 1:]:
        for call in message.get("toolCalls") or []:
            calls[str(call.get("id"))] = (f"{call.get('name')}("
                                          f"{json.dumps(call.get('args') or {}, default=str)[:200]})")
        for result in message.get("toolResults") or []:
            value = result.get("result")
            text = value if isinstance(value, str) else json.dumps(value, default=str)
            text = text if len(text) <= RESULT_CHARS else text[:RESULT_CHARS] + " …"
            lines.append(f"- {calls.get(str(result.get('id')), result.get('name'))} → {text}")
    return said, "\n".join(lines)


def _start(goal: str = "", title: str = "", kind: str = "generic", priority: int = 2,
           done_when: Any = None, context: str = "", ctx: Any = None) -> dict[str, Any]:
    goal = (goal or "").strip()
    if not goal:
        return {"ok": False, "error": "There's nothing to work on."}
    if kind not in KINDS:
        kind = "generic"
    conversation_id = _conversation_of(ctx)
    said, already = _said_and_done(conversation_id)
    brief = objective.make_brief(outcome=goal, done_when=done_when, context=context,
                                 said=said, already=already)
    try:
        job = admit(title=(title or goal)[:80], goal=goal, kind=kind,
                    priority=max(1, min(3, int(priority or 2))),
                    conversation_id=conversation_id, brief=brief)
    except AtCapacity as err:
        # The line itself is full (or the one computer is taken): name what is already going
        # so the user can say which should give way.
        running = ", ".join(f'"{j["title"]}"' for j in err.active) or "other work"
        return {"ok": False, "error": f"I've already got {running} going or waiting. "
                                      "Which of those should I drop to take this on?"}
    if job.get("waitingBehind"):
        # Taken on, and waiting its turn: said plainly, never a silent queue.
        ahead = ", ".join(f'"{title}"' for title in job["waitingBehind"][:3])
        return {"ok": True, "id": job["id"], "title": job["title"], "status": "queued",
                "waitingBehind": job["waitingBehind"],
                "speak": f"I've taken on {job['title']}. It starts as soon as {ahead} "
                         f"{'is' if len(job['waitingBehind']) == 1 else 'are'} out of the way."}
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
        description=("Take something on and see it through by yourself, in the background, "
                     "while the conversation carries on; the finished result comes back to "
                     "them. Use it when they ask you to keep at something, when the work needs "
                     "more steps than one reply has room for, or when you could not finish in "
                     "this reply — it carries on from what you have already found. Give the "
                     "whole outcome and what done looks like: the work cannot see this "
                     "conversation."),
        input_schema={"type": "object", "properties": {
            "goal": {"type": "string",
                     "description": "The outcome wanted, in full, with every name, place and "
                                    "detail they gave."},
            "done_when": {"type": "array", "items": {"type": "string"},
                          "description": "What will be true when it is finished — each a thing "
                                         "that can be checked, e.g. \"a report is filed with "
                                         "the total\"."},
            "context": {"type": "string",
                        "description": "Anything settled in this conversation that the work "
                                       "needs: preferences, limits, what to leave alone."},
            "title": {"type": "string", "description": "A short name for it."},
            "kind": {"type": "string",
                     "description": 'Leave out, except "computer" for work that must operate '
                                    "their mouse and keyboard."},
            "priority": {"type": "integer",
                         "description": "1 (most important) to 3. Default 2."}},
            # `done_when` is asked for, not demanded: a call refused for leaving it out would
            # lose the work on the very step that hands it over, and a brief without it says so.
            "required": ["goal"]},
        risk=Risk.MEDIUM, request_suffices=True, handler=_start, summarize=_start_summary,
        wants_context=True, timeout_s=20.0, tags=frozenset({"meta", "core", "handoff"}),
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
        risk=Risk.MEDIUM, request_suffices=True, handler=_stop, summarize=_stop_summary,
        timeout_s=15.0, tags=frozenset({"meta"}),
    ),
]
