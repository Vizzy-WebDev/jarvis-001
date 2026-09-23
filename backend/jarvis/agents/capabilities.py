"""The capabilities that make agents usable from a turn.

- `ask_specialist` — the ONE delegation primitive, for Jarvis and for every
  agent alike. Its declaration names the specialists currently switched on, so it
  is re-synced whenever an agent is created, edited, enabled or removed — the same
  shape as `skills/capabilities.py`, and for the same reason: the roster is not
  fixed at startup.
- `read_my_notes` / `write_my_note` — an agent's own working record. The calling
  agent is worked out from the turn's session, never taken from an argument, so an
  agent can only ever read and write its own notes.

Not under `tools/` on purpose: these reach the runner, and the runner reaches the
turn loop. The registry's loader never imports this module — `assembly.py` does,
after the loader has finished, exactly as it does for Skills and connectors.

Delegation itself is LOW risk: handing over a task does nothing outside Jarvis.
Everything the specialist then DOES is gated by that action's own declared risk,
with the same floors as anywhere else.
"""

from __future__ import annotations

import logging
from typing import Any

from ..capabilities import CapabilityRegistry, CapabilitySpec, Risk
from . import ensure_builtins, store

logger = logging.getLogger(__name__)

#: A delegated task is a whole turn of someone else's work, research included —
#: several model steps and tool calls. The executor stops WAITING after this; the
#: run itself carries on and is still recorded (`agent_runs`).
DELEGATION_TIMEOUT_S = 900.0

_NOTES_ONLY_FOR_AGENTS = "Only a specialist agent keeps notes; there is no agent behind this turn."


def _calling_agent(ctx: Any) -> str | None:
    run = store.running_run_for_session(ctx.session_id) if ctx is not None else None
    return run["agentId"] if run else None


def _ask(ctx: Any = None, agent: str = "", task: str = "", context: str | None = None,
         background: bool = False) -> dict[str, Any]:
    from . import runner

    if ctx is None:
        return {"ok": False, "error": "A specialist can only be asked from inside a turn."}
    if background:
        return _in_background(ctx, agent, task, context)
    try:
        outcome = runner.delegate(agent, task, from_session=ctx.session_id, context=context,
                                  autonomy=ctx.autonomy, surface=ctx.surface)
    except runner.AgentUnavailable as err:
        return {"ok": False, "error": str(err)}
    return outcome.as_result()


def _in_background(ctx: Any, agent: str, task: str, context: str | None) -> dict[str, Any]:
    """Long work as a background job run by the specialist — supervised, retried
    once, parked for a decision, and shown on the Jobs screen like any other job."""
    from ..jobs.orchestrator import AtCapacity, admit
    from . import runner

    try:
        target = runner.usable_agent(agent)
    except runner.AgentUnavailable as err:
        return {"ok": False, "error": str(err)}
    if store.running_run_for_session(ctx.session_id) is not None:
        return {"ok": False, "error": "A specialist can't start background work of its own; "
                                      "do this part now, or say it needs doing in the background."}
    goal = task.strip() + (f"\n\nContext you were given:\n{context.strip()}"
                           if context and context.strip() else "")
    try:
        job = admit(title=f"{target['name']}: {task.strip()}"[:80], goal=goal,
                    conversation_id=ctx.session_id, agent_id=target["id"])
    except AtCapacity as err:
        running = ", ".join(f'"{j["title"]}"' for j in err.active) or "other work"
        return {"ok": False, "error": f"Already working on {running}. Which should give way?"}
    return {"ok": True, "background": True, "jobId": job["id"], "agent": target["name"],
            "status": job["status"],
            "speak": f"{target['name']} is working on that in the background."}


def _read_notes(ctx: Any = None, topic: str | None = None) -> dict[str, Any]:
    agent_id = _calling_agent(ctx)
    if agent_id is None:
        return {"ok": False, "error": _NOTES_ONLY_FOR_AGENTS}
    notes = store.list_notes(agent_id, topic)
    if not notes:
        return {"ok": True, "notes": [], "note": "Nothing kept under that yet." if topic
                else "No notes kept yet."}
    return {"ok": True, "notes": [{"topic": n["topic"], "text": n["text"],
                                   "updated": n["updatedAt"]} for n in notes[:20]]}


def _write_note(ctx: Any = None, topic: str = "", text: str = "",
                mode: str = "replace") -> dict[str, Any]:
    agent_id = _calling_agent(ctx)
    if agent_id is None:
        return {"ok": False, "error": _NOTES_ONLY_FOR_AGENTS}
    try:
        note = store.write_note(agent_id, topic, text,
                                mode="append" if mode == "append" else "replace")
    except store.AgentError as err:
        return {"ok": False, "error": str(err)}
    return {"ok": True, "topic": note["topic"], "saved": True}


def _roster_text(agents: list[dict[str, Any]]) -> str:
    return "\n".join(f"- {a['id']}: {a['name']} — {a['description']}" for a in agents)


def ask_spec(agents: list[dict[str, Any]]) -> CapabilitySpec:
    return CapabilitySpec(
        id="agents.ask_specialist", name="ask_specialist",
        description=(
            "Hand a task to one of your specialist agents and get their finished work back. "
            "Use it when the work genuinely calls for a specialist's depth; answer directly "
            "when it doesn't. You can ask several in turn and combine what they give you. "
            "Give them the full task and any context they need — they cannot see this "
            "conversation. Set background to true for long work that shouldn't hold up the "
            "reply. The specialists:\n" + _roster_text(agents)),
        input_schema={"type": "object", "properties": {
            "agent": {"type": "string", "description": "Which specialist, by id or name.",
                      "enum": [a["id"] for a in agents]},
            "task": {"type": "string", "description": "What they should do, in full."},
            "context": {"type": "string",
                        "description": "Anything they need to know that isn't in the task: "
                                       "what the operator said, earlier results."},
            "background": {"type": "boolean",
                           "description": "True to run it as background work instead of "
                                          "waiting for the result now."}},
            "required": ["agent", "task"]},
        risk=Risk.LOW, handler=_ask, wants_context=True, timeout_s=DELEGATION_TIMEOUT_S,
        tags=frozenset({"core"}),
    )


NOTE_SPECS = [
    CapabilitySpec(
        id="agents.read_my_notes", name="read_my_notes",
        description=("Read your own working notes, kept between tasks — a learner's plan and "
                     "progress, what you have already found or surfaced. Optionally one topic."),
        input_schema={"type": "object", "properties": {
            "topic": {"type": "string", "description": "Just this topic; leave out for all."}},
            "required": []},
        risk=Risk.LOW, handler=_read_notes, wants_context=True, timeout_s=10.0,
    ),
    CapabilitySpec(
        id="agents.write_my_note", name="write_my_note",
        description=("Keep something in your own working notes under a topic, so it is there "
                     "next time. mode 'replace' (default) overwrites that topic; 'append' adds "
                     "to it, for a running log."),
        input_schema={"type": "object", "properties": {
            "topic": {"type": "string", "description": "A short topic name you'll reuse."},
            "text": {"type": "string", "description": "What to keep."},
            "mode": {"type": "string", "enum": ["replace", "append"],
                     "description": "replace (default) or append."}},
            "required": ["topic", "text"]},
        risk=Risk.LOW, handler=_write_note, wants_context=True, timeout_s=10.0,
    ),
]


def sync(registry: CapabilityRegistry) -> list[str]:
    """Register the agent capabilities for the current roster. Called when the
    registry is built and after anything changes an agent."""
    ensure_builtins()
    for spec in NOTE_SPECS:
        registry.register(spec)
    enabled = store.list_agents(include_disabled=False)
    if enabled:
        registry.register(ask_spec(enabled))
    else:
        registry.unregister("ask_specialist")
    return [a["id"] for a in enabled]


def close_orphaned_runs() -> int:
    """At startup: a run still marked running has nothing running it any more."""
    orphans = [r for r in store.list_runs(limit=500) if r["status"] == "running"]
    for run in orphans:
        store.finish_run(run["id"], status="failed", result=run.get("result"),
                         error="Jarvis restarted before this finished.",
                         tools_used=run.get("toolsUsed"))
    return len(orphans)
