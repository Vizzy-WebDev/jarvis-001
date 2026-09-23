"""Running an agent — every agent, built-in or custom, through this one path.

An agent run is an ordinary turn (`Orchestrator.run_turn`) with three things set
on the request: WHO it runs as (`AgentBrief`: identity, doctrine, guardrails),
what it may use (`allowed_names`, enforced by the policy layer like any other
allowlist) and which model (`model_id`, the same found-or-refused pin a scheduled
task uses). Nothing else about a turn changes — the same executor, the same
approval floors, the same model system. That is what keeps a specialist from
being a second assistant with its own loop.

**Delegation** (`delegate()`) is the one way work reaches an agent from a turn.
It works out who is asking from the calling turn's session: Jarvis's own session
has no run behind it; an agent's session has the run that is driving it. The
guards live here, in code, not in any prompt: an agent may only ask the
collaborators it was given, a chain may not come back round to an agent already
in it, it may not go deeper than `MAX_DEPTH`, and one request may not fan out
into more than `MAX_RUNS_PER_ROOT` runs.

**Threads.** A delegation waits for a whole nested turn, from inside a capability
call. The nested turn runs on its own named thread (`agent-run-…`), and
`capabilities/execute.py` gives such threads their own executor pool, so a
waiting delegation never holds a worker the work it is waiting for would need.

Lazy imports of `assembly` inside functions, same reason as `jobs/worker.py`:
`tools/agent_tools.py` imports this module, and everything under `tools/` is
loaded by the registry's loader.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Iterator

from .. import conversation
from ..events import EventType, bus as default_bus
from ..orchestrator.context import AgentBrief
from ..policy import Autonomy, Surface
from . import ensure_builtins, store
from .builtins import COMMON_GUARDRAILS

logger = logging.getLogger(__name__)

#: Jarvis → A → B → C, and no further. Deep chains cost time and quota and are
#: almost always a model passing work around rather than doing it.
MAX_DEPTH = 3

#: Every run started on behalf of one request, the first one included.
MAX_RUNS_PER_ROOT = 8

#: The tools every agent has as its own, whatever its access says: finding a
#: capability it may use, and its notes. `ask_specialist` is added only when the
#: agent has somebody to ask.
OWN_TOOLS = ("find_capability", "read_my_notes", "write_my_note")
DELEGATE_TOOL = "ask_specialist"

#: How many earlier task/result pairs re-seed an agent's session after a restart,
#: so "the quiz you set me" still means something.
RESEED_RUNS = 3

SESSION_PREFIX = "agent:"


class AgentUnavailable(RuntimeError):
    """This agent cannot take work, and why — in words fit to show."""


def session_for(agent_id: str, conversation_id: str | None) -> str:
    return f"{SESSION_PREFIX}{agent_id}:{conversation_id or 'solo'}"


# --- who an agent is, and what it may use -------------------------------------

def collaborators_of(agent: dict[str, Any]) -> list[dict[str, Any]]:
    """The ENABLED agents this one may ask, never itself."""
    wanted = agent.get("collaborators")
    others = [a for a in store.list_agents(include_disabled=False) if a["id"] != agent["id"]]
    if wanted == "any":
        return others
    allowed = set(wanted or [])
    return [a for a in others if a["id"] in allowed]


def brief_for(agent: dict[str, Any], *, direct: bool = False) -> AgentBrief:
    guardrails = "\n".join(p for p in (agent.get("guardrails", "").strip(), COMMON_GUARDRAILS) if p)
    return AgentBrief(
        agent_id=agent["id"], name=agent["name"], mission=agent.get("mission") or "",
        doctrine=agent.get("doctrine") or "", guardrails=guardrails,
        memory=agent.get("memoryAccess") != "none",
        collaborators=tuple((a["id"], a["name"], a.get("description") or "")
                            for a in collaborators_of(agent)),
        direct=direct)


def access_for(agent: dict[str, Any], registry: Any) -> tuple[frozenset[str] | None, frozenset[str]]:
    """(what the agent may call, what is declared to it up front).

    Resolved at run time, never saved as names: a connector's tool list changes
    when it reconnects and Skills come and go. `mode: all` means no allowlist at
    all — the policy layer still gates every call by its declared risk.
    """
    from ..capabilities import CapabilityKind

    access = store.normalize_access(agent.get("capabilityAccess"))
    own = set(OWN_TOOLS)
    if collaborators_of(agent):
        own.add(DELEGATE_TOOL)
    declared = frozenset({*access["names"], *own})
    if access["mode"] == "all":
        return None, declared

    skills = {s.name for s in registry.list(kind=CapabilityKind.SKILL)}
    if access["connectors"] == "all":
        connector_tools = {s.name for s in registry.list(kind=CapabilityKind.CONNECTOR)}
    else:
        from ..connectors.capabilities import tool_names_for

        connector_tools = {name for cid in access["connectors"] for name in tool_names_for(cid)}
    allowed = frozenset({*access["names"], *own, *skills, *connector_tools})
    return allowed, declared


# --- one run ------------------------------------------------------------------

@dataclass
class RunOutcome:
    run: dict[str, Any]
    status: str
    result: str = ""
    error: str | None = None
    approval: dict[str, Any] | None = None
    model_id: str | None = None
    tools_used: list[str] = field(default_factory=list)

    def as_result(self) -> dict[str, Any]:
        """What a delegating turn is told — plain data a model can reason over."""
        agent = store.get_agent(self.run["agentId"]) or {"name": self.run["agentId"]}
        out: dict[str, Any] = {"ok": self.status == "done", "agent": agent["name"],
                               "runId": self.run["id"], "status": self.status}
        if self.result:
            out["result"] = self.result
        if self.error:
            out["error"] = self.error
        if self.tools_used:
            out["toolsUsed"] = sorted(set(self.tools_used))
        delegated = [r for r in store.list_runs(root_run_id=self.run["rootRunId"])
                     if r["parentRunId"] == self.run["id"]]
        if delegated:
            out["askedForHelp"] = [
                {"agent": (store.get_agent(r["agentId"]) or {}).get("name", r["agentId"]),
                 "status": r["status"]} for r in reversed(delegated)]
        if self.approval:
            # Read by the turn loop (`_approval_of`), which puts the question in
            # front of the person as a real approval — the agent cannot answer it.
            out["approval"] = self.approval
        return out


_session_locks: dict[str, threading.Lock] = {}
_session_locks_guard = threading.Lock()


def _lock_for(session_id: str) -> threading.Lock:
    with _session_locks_guard:
        return _session_locks.setdefault(session_id, threading.Lock())


def _reseed(session_id: str) -> None:
    """After a restart the in-memory session is gone; put the last few task/result
    pairs back so the agent's own recent work is still in front of it."""
    if conversation.get_messages(session_id):
        return
    earlier = [r for r in store.list_runs(session_id=session_id, limit=RESEED_RUNS * 3)
               if r["status"] == "done" and r["result"]][:RESEED_RUNS]
    for run in reversed(earlier):
        conversation.push_user_text(session_id, run["task"])
        conversation.push_assistant_text(session_id, run["result"])


def _surface_for(caller: Surface) -> Surface:
    """A specialist's own turn is never spoken and never a job's own voice: it is
    background work for whoever asked. Autonomy, not surface, carries who can be asked."""
    return Surface.SCHEDULED if caller in (Surface.JOB, Surface.SCHEDULED) else Surface.TEXT


def _message_for(task: str, context: str | None, requested_by: str) -> str:
    asker = "Jarvis" if requested_by in ("jarvis", "operator") else (
        (store.get_agent(requested_by) or {}).get("name") or requested_by)
    parts = [f"Task from {asker}:\n{task.strip()}"]
    if context and context.strip():
        parts.append(f"Context you were given:\n{context.strip()}")
    return "\n\n".join(parts)


def stream_run(agent: dict[str, Any], run: dict[str, Any], message: str, *,
               autonomy: Autonomy, surface: Surface, direct: bool = False,
               attachments: tuple[str, ...] = (), event_bus: Any = None) -> Iterator[Any]:
    """Run an already-recorded agent run's turn, yielding its events as they happen
    and recording how it ended. The chat's "talk directly" path streams this to the
    browser; `run_agent` consumes it."""
    from ..assembly import get_orchestrator, get_registry
    from ..orchestrator import ApprovalRequired, Done, Failed, Interrupted, ToolRan, TurnRequest

    ebus = event_bus or default_bus
    allowed, declared = access_for(agent, get_registry())
    request = TurnRequest(
        text=message, session_id=run["sessionId"], surface=surface, autonomy=autonomy,
        allowed_names=allowed, always_declare=declared, model_id=agent.get("modelPin") or None,
        agent=brief_for(agent, direct=direct), attachments=attachments)

    status, result, error, approval, model_id = "failed", "", None, None, None
    tools: list[str] = []
    try:
        for event in get_orchestrator().run_turn(request):
            if isinstance(event, ToolRan):
                tools.append(event.capability)
            elif isinstance(event, Done):
                status, result, model_id = "done", event.text, event.model_id
            elif isinstance(event, Failed):
                status, error = "failed", event.error
            elif isinstance(event, Interrupted):
                status, error = "failed", "It was interrupted before finishing."
            elif isinstance(event, ApprovalRequired):
                status = "awaiting_approval"
                approval = {"id": event.approval_id, "capability": event.capability,
                            "reason": event.reason}
            yield event
    except Exception as err:  # noqa: BLE001 — a run must always be closed off
        logger.exception("agent run %s crashed", run["id"])
        status, error = "failed", f"It stopped with an error: {err}"
        raise
    finally:
        if status == "failed" and not error:
            error = "It ended without an answer."
        finished = store.finish_run(run["id"], status=status, result=result or None, error=error,
                                    approval_id=(approval or {}).get("id"), model_id=model_id,
                                    tools_used=tools)
        ebus.publish(EventType.AGENT_RUN_FINISHED, {
            "runId": run["id"], "agentId": agent["id"], "agentName": agent["name"],
            "status": status, "parentRunId": run["parentRunId"], "rootRunId": run["rootRunId"],
            "conversationId": run["conversationId"], "error": error})
        run.update(finished)


def start_run(agent: dict[str, Any], task: str, *, session_id: str, requested_by: str,
              conversation_id: str | None, parent: dict[str, Any] | None = None,
              job_id: str | None = None, event_bus: Any = None) -> dict[str, Any]:
    run = store.create_run(
        agent_id=agent["id"], task=task, session_id=session_id, requested_by=requested_by,
        conversation_id=conversation_id, parent_run_id=(parent or {}).get("id"),
        root_run_id=(parent or {}).get("rootRunId"), depth=(parent or {}).get("depth", 0) + 1,
        job_id=job_id)
    (event_bus or default_bus).publish(EventType.AGENT_RUN_STARTED, {
        "runId": run["id"], "agentId": agent["id"], "agentName": agent["name"],
        "requestedBy": requested_by, "parentRunId": run["parentRunId"],
        "rootRunId": run["rootRunId"], "conversationId": conversation_id, "task": task[:200]})
    return run


def usable_agent(which: str) -> dict[str, Any]:
    ensure_builtins()
    agent = store.find_agent(which)
    if agent is None:
        names = ", ".join(a["name"] for a in store.list_agents(include_disabled=False))
        raise AgentUnavailable(f'There is no specialist called "{which}". Available: {names}.')
    if not agent["enabled"]:
        raise AgentUnavailable(f"{agent['name']} is turned off. It can be turned back on on the "
                               "Specialists screen.")
    return agent


def run_agent(which: str, task: str, *, requested_by: str = "jarvis",
              conversation_id: str | None = None, context: str | None = None,
              parent: dict[str, Any] | None = None,
              autonomy: Autonomy = Autonomy.INTERACTIVE, surface: Surface = Surface.TEXT,
              job_id: str | None = None, session_id: str | None = None,
              event_bus: Any = None) -> RunOutcome:
    """Run one agent on one task to its end, and record it. Blocks until done.

    The turn runs on its own named thread (see the module header); this call just
    waits for it. Two runs in the same agent session take turns, because they
    would otherwise interleave one transcript.
    """
    agent = usable_agent(which)
    session = session_id or session_for(agent["id"], conversation_id)
    run = start_run(agent, task, session_id=session, requested_by=requested_by,
                    conversation_id=conversation_id, parent=parent, job_id=job_id,
                    event_bus=event_bus)
    message = _message_for(task, context, requested_by)
    holder: dict[str, Any] = {}

    def go() -> None:
        try:
            with _lock_for(session):
                _reseed(session)
                for _ in stream_run(agent, run, message, autonomy=autonomy,
                                    surface=_surface_for(surface), event_bus=event_bus):
                    pass
        except Exception as err:  # noqa: BLE001 — reported through the run record
            holder["error"] = str(err)

    thread = threading.Thread(target=go, name=f"agent-run-{run['id']}", daemon=True)
    thread.start()
    thread.join()
    final = store.get_run(run["id"]) or run
    approval = None
    if final["status"] == "awaiting_approval" and final.get("approvalId"):
        from ..policy import approvals as approvals_store

        pending = approvals_store.get(final["approvalId"])
        approval = {"id": final["approvalId"],
                    "capability": pending.capability if pending else "",
                    "reason": pending.reason if pending else ""}
    return RunOutcome(run=final, status=final["status"], result=final.get("result") or "",
                      error=final.get("error") or holder.get("error"), approval=approval,
                      model_id=final.get("modelId"), tools_used=final.get("toolsUsed") or [])


# --- delegation ---------------------------------------------------------------

def _chain_agents(run: dict[str, Any] | None) -> list[str]:
    """The agents from this run up to the root, nearest first."""
    out: list[str] = []
    while run is not None:
        out.append(run["agentId"])
        run = store.get_run(run["parentRunId"]) if run.get("parentRunId") else None
    return out


def delegate(which: str, task: str, *, from_session: str, context: str | None = None,
             autonomy: Autonomy = Autonomy.INTERACTIVE, surface: Surface = Surface.TEXT,
             event_bus: Any = None) -> RunOutcome:
    """Hand a task to a specialist from inside a turn, with every guard applied.

    Raises `AgentUnavailable` with a plain reason when the delegation must not
    happen; the caller (the `ask_specialist` tool) turns that into its result.
    """
    if not (task or "").strip():
        raise AgentUnavailable("There's no task to hand over.")
    target = usable_agent(which)
    caller = store.running_run_for_session(from_session)

    if caller is None:
        # Jarvis itself (or a direct chat with no agent run behind it): a new root.
        return run_agent(target["id"], task, requested_by="jarvis", conversation_id=from_session,
                         context=context, autonomy=autonomy, surface=surface,
                         event_bus=event_bus)

    asker = store.get_agent(caller["agentId"])
    if asker is None:
        raise AgentUnavailable("The agent asking for help no longer exists.")
    if target["id"] not in {a["id"] for a in collaborators_of(asker)}:
        raise AgentUnavailable(f"{asker['name']} isn't set up to ask {target['name']} for help.")
    if target["id"] in _chain_agents(caller):
        raise AgentUnavailable(f"{target['name']} is already part of this piece of work, so "
                               "asking it again would go round in a circle. Do this part yourself.")
    if caller["depth"] + 1 > MAX_DEPTH:
        raise AgentUnavailable("This has already been handed down as far as it can go. Do this "
                               "part yourself.")
    if store.count_runs_under(caller["rootRunId"]) >= MAX_RUNS_PER_ROOT:
        raise AgentUnavailable("This request has already involved as many specialists as it can. "
                               "Finish with what you have.")
    return run_agent(target["id"], task, requested_by=asker["id"],
                     conversation_id=caller["conversationId"], context=context, parent=caller,
                     autonomy=autonomy, surface=surface, event_bus=event_bus)
