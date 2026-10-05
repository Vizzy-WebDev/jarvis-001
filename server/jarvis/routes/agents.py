"""Specialist agents over HTTP — the Specialists screen's whole surface.

A surface over `jarvis/agents/`, which holds every rule about what an agent is.
Built-in and custom agents come back from the same routes in the same shape; the
only difference a caller sees is `builtin`, which decides whether reset or delete
is offered — and the store, not this file, refuses the wrong one.

**Every change re-syncs the live registry** (`agents/capabilities.sync`), because
`ask_specialist` names the enabled roster in its declaration: an agent switched
off here must stop being offered on the very next turn, not after a restart.

**The ability picker (`/api/agents/abilities`) is an access list, not a catalogue
of Skills.** It returns three separately-sourced groups: built-in abilities from
the registry, Skills ONLY from `skills/files.list_user_skills()` (the one source
that structurally cannot return a built-in), and connectors from their own store.
The screen labels them as what they are.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from ..agents import ensure_builtins, store
from ..agents.builtins import builtin

router = APIRouter(prefix="/api")

#: The agents' own tools, which every agent has anyway — offering them in the
#: picker would suggest they can be taken away.
_OWN = {"find_capability", "read_my_notes", "write_my_note", "ask_specialist"}


def _sync() -> None:
    from ..agents.capabilities import sync
    from ..assembly import get_registry

    sync(get_registry())


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status)


def _with_last_run(agent: dict[str, Any]) -> dict[str, Any]:
    runs = store.list_runs(agent_id=agent["id"], limit=1)
    return {**agent, "lastRun": _run_summary(runs[0]) if runs else None}


def _run_summary(run: dict[str, Any]) -> dict[str, Any]:
    return {k: run[k] for k in ("id", "agentId", "status", "task", "requestedBy",
                                "startedAt", "finishedAt", "depth", "parentRunId",
                                "rootRunId", "error")}


def _fields(body: dict[str, Any]) -> dict[str, Any]:
    return {k: body[k] for k in store.EDITABLE if k in body}


@router.get("/agents")
def listed() -> dict[str, Any]:
    ensure_builtins()
    return {"agents": [_with_last_run(a) for a in store.list_agents()]}


@router.post("/agents")
def create(body: dict[str, Any] = Body(default_factory=dict)):
    ensure_builtins()
    try:
        agent = store.create_agent(_fields(body))
    except store.AgentError as err:
        return _error(str(err))
    _sync()
    return {"ok": True, "agent": agent}


@router.get("/agents/abilities")
def abilities() -> dict[str, Any]:
    """What an agent's access can be made of, grouped by what each thing is."""
    from ..assembly import get_registry
    from ..capabilities import CapabilityKind
    from ..connectors import store as connector_store
    from ..skills import files as skill_files

    built_in = [{"name": s.name, "description": s.description, "risk": s.risk.value}
                for s in get_registry().list(kind=CapabilityKind.TOOL)
                if not s.has_tag("job") and s.name not in _OWN]
    skills = [{"name": s["name"], "description": s.get("description") or ""}
              for s in skill_files.list_user_skills() if s.get("enabled")]
    connectors = [{"id": c["id"], "label": c.get("label") or c.get("type") or c["id"],
                   "type": c.get("type")}
                  for c in connector_store.list_connectors() if c.get("enabled", True)]
    return {"builtIn": built_in, "skills": skills, "connectors": connectors}


@router.get("/agents/{agent_id}")
def detail(agent_id: str):
    ensure_builtins()
    agent = store.get_agent(agent_id)
    if agent is None:
        return _error("There's no agent by that name.", 404)
    return {"agent": agent, "runs": [_run_summary(r) for r in store.list_runs(
        agent_id=agent_id, limit=30)], "notes": store.list_notes(agent_id),
        "hasDefault": builtin(agent_id) is not None}


@router.patch("/agents/{agent_id}")
def update(agent_id: str, body: dict[str, Any] = Body(default_factory=dict)):
    ensure_builtins()
    try:
        agent = store.update_agent(agent_id, _fields(body))
    except store.AgentError as err:
        return _error(str(err), 404 if store.get_agent(agent_id) is None else 400)
    _sync()
    return {"ok": True, "agent": agent}


@router.delete("/agents/{agent_id}")
def delete(agent_id: str):
    ensure_builtins()
    try:
        store.delete_agent(agent_id)
    except store.AgentError as err:
        return _error(str(err), 404 if store.get_agent(agent_id) is None else 400)
    _sync()
    return {"ok": True}


@router.post("/agents/{agent_id}/reset")
def reset(agent_id: str):
    ensure_builtins()
    definition = builtin(agent_id)
    if definition is None:
        return _error("Only a built-in agent can be reset to its default.")
    agent = store.reset_builtin(agent_id, definition)
    _sync()
    return {"ok": True, "agent": agent}


@router.get("/agent-runs/{run_id}")
def run_detail(run_id: str):
    """One run with everything it led to — the whole delegation tree under its root."""
    run = store.get_run(run_id)
    if run is None:
        return _error("That run no longer exists.", 404)
    tree = store.list_runs(root_run_id=run["rootRunId"], limit=100)
    names = {a["id"]: a["name"] for a in store.list_agents()}
    return {"run": {**run, "agentName": names.get(run["agentId"], run["agentId"])},
            "tree": [{**r, "agentName": names.get(r["agentId"], r["agentId"])}
                     for r in reversed(tree)]}


@router.patch("/agents/{agent_id}/notes/{note_id}")
def edit_note(agent_id: str, note_id: str, body: dict[str, Any] = Body(default_factory=dict)):
    try:
        note = store.update_note(note_id, topic=body.get("topic"), text=body.get("text"))
    except store.AgentError as err:
        return _error(str(err), 404)
    return {"ok": True, "note": note}


@router.delete("/agents/{agent_id}/notes/{note_id}")
def delete_note(agent_id: str, note_id: str):
    store.delete_note(note_id)
    return {"ok": True}
