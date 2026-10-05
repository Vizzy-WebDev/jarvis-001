"""Agents, their runs and their notes — rows, and nothing but rows.

A leaf: it imports the database and the clock and nothing else, so the runner,
the routes and the tools can all read it without a cycle.

**One table for every agent.** A built-in agent is a row seeded from
`builtins.py`; a custom one is a row the person made. The `builtin` flag only
decides what the interface offers (reset versus delete). Nothing that RUNS an
agent looks at it, which is what keeps custom agents from ever becoming a second
architecture.

Every function takes the module lock: the database is one connection shared by
every server thread (see `db.py`), and the other stores over it do the same.
"""

from __future__ import annotations

import functools
import json
import re
import threading
import uuid
from typing import Any, Callable

from ..db import get_db
from ..jscompat import now_iso

_lock = threading.RLock()

#: What `capability_access` looks like when nothing more specific was said.
DEFAULT_ACCESS: dict[str, Any] = {"mode": "selected", "names": [], "connectors": "all"}

MEMORY_ACCESS = ("none", "read")

RUN_STATUSES = ("running", "done", "failed", "awaiting_approval")

_SLUG = re.compile(r"[^a-z0-9]+")

#: The editable fields and nothing else — an update naming anything outside this
#: set (an id, the builtin flag) is ignored rather than trusted.
EDITABLE = ("name", "description", "mission", "doctrine", "guardrails", "modelPin",
            "capabilityAccess", "memoryAccess", "collaborators", "enabled")


def _locked(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def inner(*args: Any, **kwargs: Any) -> Any:
        with _lock:
            return fn(*args, **kwargs)

    return inner


class AgentError(ValueError):
    """A request about an agent that cannot be honoured, in words fit to show."""


# --- shape -------------------------------------------------------------------

def normalize_access(value: Any) -> dict[str, Any]:
    """A capability-access value in its one canonical shape, whatever arrived."""
    raw = value if isinstance(value, dict) else {}
    mode = raw.get("mode") if raw.get("mode") in ("all", "selected") else "selected"
    names = sorted({str(n) for n in (raw.get("names") or []) if isinstance(n, str) and n.strip()})
    connectors = raw.get("connectors", "all")
    if connectors != "all":
        connectors = sorted({str(c) for c in (connectors or []) if isinstance(c, str) and c})
    return {"mode": mode, "names": names, "connectors": connectors}


def normalize_collaborators(value: Any) -> str | list[str]:
    if value == "any":
        return "any"
    return sorted({str(v) for v in (value or []) if isinstance(v, str) and v})


def _agent(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    collaborators = json.loads(row["collaborators"])
    return {
        "id": row["id"], "name": row["name"], "description": row["description"],
        "mission": row["mission"], "doctrine": row["doctrine"],
        "guardrails": row["guardrails"], "modelPin": row["model_pin"],
        "capabilityAccess": normalize_access(json.loads(row["capability_access"])),
        "memoryAccess": row["memory_access"],
        "collaborators": normalize_collaborators(collaborators),
        "enabled": bool(row["enabled"]), "builtin": bool(row["builtin"]),
        "builtinVersion": row["builtin_version"],
        "createdAt": row["created_at"], "updatedAt": row["updated_at"],
    }


def _validate(fields: dict[str, Any]) -> None:
    if "name" in fields and not str(fields["name"] or "").strip():
        raise AgentError("An agent needs a name.")
    if "memoryAccess" in fields and fields["memoryAccess"] not in MEMORY_ACCESS:
        raise AgentError('Memory access is "none" or "read".')


def slug_for(name: str) -> str:
    return _SLUG.sub("-", str(name or "").lower()).strip("-")[:40] or "agent"


# --- agents ------------------------------------------------------------------

@_locked
def list_agents(*, include_disabled: bool = True) -> list[dict[str, Any]]:
    sql = "SELECT * FROM agents" + ("" if include_disabled else " WHERE enabled = 1")
    rows = get_db().execute(sql + " ORDER BY builtin DESC, created_at, name").fetchall()
    return [_agent(r) for r in rows]  # type: ignore[misc]


@_locked
def get_agent(agent_id: str) -> dict[str, Any] | None:
    return _agent(get_db().execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone())


@_locked
def find_agent(which: str) -> dict[str, Any] | None:
    """An agent by id, or by name however it was typed ("research", "The Teacher")."""
    wanted = str(which or "").strip()
    if not wanted:
        return None
    exact = get_agent(wanted)
    if exact is not None:
        return exact
    lowered = wanted.lower()
    agents = list_agents()
    for agent in agents:
        if agent["name"].lower() == lowered or agent["id"] == slug_for(wanted):
            return agent
    matches = [a for a in agents if lowered in a["name"].lower() or lowered in a["id"]]
    return matches[0] if len(matches) == 1 else None


def _insert(agent_id: str, fields: dict[str, Any], *, builtin: bool,
            builtin_version: int | None, stamp: str) -> None:
    get_db().execute(
        "INSERT INTO agents (id, name, description, mission, doctrine, guardrails, model_pin, "
        "capability_access, memory_access, collaborators, enabled, builtin, builtin_version, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (agent_id, str(fields["name"]).strip(), fields.get("description") or "",
         fields.get("mission") or "", fields.get("doctrine") or "",
         fields.get("guardrails") or "", fields.get("modelPin") or None,
         json.dumps(normalize_access(fields.get("capabilityAccess", DEFAULT_ACCESS))),
         fields.get("memoryAccess") or "read",
         json.dumps(normalize_collaborators(fields.get("collaborators", "any"))),
         0 if fields.get("enabled") is False else 1, 1 if builtin else 0, builtin_version,
         stamp, stamp))


@_locked
def create_agent(fields: dict[str, Any]) -> dict[str, Any]:
    """A new custom agent. Its id is a slug of its name, made unique."""
    _validate({"name": fields.get("name"), **fields})
    base = slug_for(fields["name"])
    agent_id, n = base, 2
    while get_agent(agent_id) is not None:
        agent_id, n = f"{base}-{n}", n + 1
    _insert(agent_id, fields, builtin=False, builtin_version=None, stamp=now_iso())
    return get_agent(agent_id)  # type: ignore[return-value]


@_locked
def update_agent(agent_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    existing = get_agent(agent_id)
    if existing is None:
        raise AgentError("There's no agent by that name.")
    changes = {k: v for k, v in fields.items() if k in EDITABLE}
    _validate(changes)
    merged = {**existing, **changes}
    get_db().execute(
        "UPDATE agents SET name = ?, description = ?, mission = ?, doctrine = ?, guardrails = ?, "
        "model_pin = ?, capability_access = ?, memory_access = ?, collaborators = ?, enabled = ?, "
        "updated_at = ? WHERE id = ?",
        (str(merged["name"]).strip(), merged["description"] or "", merged["mission"] or "",
         merged["doctrine"] or "", merged["guardrails"] or "", merged["modelPin"] or None,
         json.dumps(normalize_access(merged["capabilityAccess"])), merged["memoryAccess"],
         json.dumps(normalize_collaborators(merged["collaborators"])),
         1 if merged["enabled"] else 0, now_iso(), agent_id))
    return get_agent(agent_id)  # type: ignore[return-value]


@_locked
def delete_agent(agent_id: str) -> None:
    existing = get_agent(agent_id)
    if existing is None:
        raise AgentError("There's no agent by that name.")
    if existing["builtin"]:
        raise AgentError(f"{existing['name']} is built in, so it can be turned off or reset, "
                         "not deleted.")
    db = get_db()
    db.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
    db.execute("DELETE FROM agent_notes WHERE agent_id = ?", (agent_id,))


@_locked
def seed_builtins(definitions: list[dict[str, Any]]) -> list[str]:
    """Make sure every built-in exists. Returns the ids it inserted or refreshed.

    A missing one is inserted. One the person never edited is refreshed when its
    definition's version moves on; one they DID edit is left exactly as they left
    it — their edit is theirs, and "Reset to default" is how they take the new one.
    """
    touched: list[str] = []
    for definition in definitions:
        existing = get_agent(definition["id"])
        version = int(definition.get("version") or 1)
        if existing is None:
            _insert(definition["id"], definition, builtin=True, builtin_version=version,
                    stamp=now_iso())
            touched.append(definition["id"])
        elif ((existing["builtinVersion"] or 0) < version
              and existing["createdAt"] == existing["updatedAt"]):
            _replace_with(definition)
            touched.append(definition["id"])
    return touched


def _replace_with(definition: dict[str, Any]) -> None:
    stamp = now_iso()
    get_db().execute(
        "UPDATE agents SET name = ?, description = ?, mission = ?, doctrine = ?, guardrails = ?, "
        "model_pin = NULL, capability_access = ?, memory_access = ?, collaborators = ?, "
        "enabled = 1, builtin_version = ?, created_at = ?, updated_at = ? WHERE id = ?",
        (definition["name"], definition.get("description") or "", definition.get("mission") or "",
         definition.get("doctrine") or "", definition.get("guardrails") or "",
         json.dumps(normalize_access(definition.get("capabilityAccess", DEFAULT_ACCESS))),
         definition.get("memoryAccess") or "read",
         json.dumps(normalize_collaborators(definition.get("collaborators", "any"))),
         int(definition.get("version") or 1), stamp, stamp, definition["id"]))


@_locked
def reset_builtin(agent_id: str, definition: dict[str, Any]) -> dict[str, Any]:
    existing = get_agent(agent_id)
    if existing is None or not existing["builtin"]:
        raise AgentError("Only a built-in agent can be reset to its default.")
    _replace_with(definition)
    return get_agent(agent_id)  # type: ignore[return-value]


# --- runs --------------------------------------------------------------------

def _run(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row["id"], "agentId": row["agent_id"], "parentRunId": row["parent_run_id"],
        "rootRunId": row["root_run_id"], "conversationId": row["conversation_id"],
        "sessionId": row["session_id"], "requestedBy": row["requested_by"],
        "task": row["task"], "status": row["status"], "result": row["result"],
        "error": row["error"], "approvalId": row["approval_id"], "modelId": row["model_id"],
        "toolsUsed": json.loads(row["tools_used"]) if row["tools_used"] else [],
        "depth": row["depth"], "jobId": row["job_id"],
        "startedAt": row["started_at"], "finishedAt": row["finished_at"],
    }


@_locked
def create_run(*, agent_id: str, task: str, session_id: str, requested_by: str,
               conversation_id: str | None = None, parent_run_id: str | None = None,
               root_run_id: str | None = None, depth: int = 1,
               job_id: str | None = None) -> dict[str, Any]:
    run_id = f"arun_{uuid.uuid4().hex[:12]}"
    get_db().execute(
        "INSERT INTO agent_runs (id, agent_id, parent_run_id, root_run_id, conversation_id, "
        "session_id, requested_by, task, status, depth, job_id, started_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)",
        (run_id, agent_id, parent_run_id, root_run_id or run_id, conversation_id, session_id,
         requested_by, task, depth, job_id, now_iso()))
    return get_run(run_id)  # type: ignore[return-value]


@_locked
def finish_run(run_id: str, *, status: str, result: str | None = None,
               error: str | None = None, approval_id: str | None = None,
               model_id: str | None = None, tools_used: list[str] | None = None) -> dict[str, Any]:
    if status not in RUN_STATUSES:
        raise ValueError(f"unknown run status {status!r}")
    get_db().execute(
        "UPDATE agent_runs SET status = ?, result = ?, error = ?, approval_id = ?, model_id = ?, "
        "tools_used = ?, finished_at = ? WHERE id = ?",
        (status, result, error, approval_id, model_id, json.dumps(tools_used or []),
         None if status == "running" else now_iso(), run_id))
    return get_run(run_id)  # type: ignore[return-value]


@_locked
def get_run(run_id: str) -> dict[str, Any] | None:
    return _run(get_db().execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone())


@_locked
def running_run_for_session(session_id: str) -> dict[str, Any] | None:
    """The run currently driving this session, if any — how a delegation made from
    INSIDE an agent's turn finds out which agent is asking."""
    return _run(get_db().execute(
        "SELECT * FROM agent_runs WHERE session_id = ? AND status = 'running' "
        "ORDER BY started_at DESC LIMIT 1", (session_id,)).fetchone())


@_locked
def list_runs(*, agent_id: str | None = None, root_run_id: str | None = None,
              session_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    clauses, params = [], []
    if agent_id:
        clauses.append("agent_id = ?")
        params.append(agent_id)
    if root_run_id:
        clauses.append("root_run_id = ?")
        params.append(root_run_id)
    if session_id:
        clauses.append("session_id = ?")
        params.append(session_id)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = get_db().execute(
        f"SELECT * FROM agent_runs{where} ORDER BY started_at DESC LIMIT ?",
        (*params, limit)).fetchall()
    return [_run(r) for r in rows]  # type: ignore[misc]


@_locked
def count_runs_under(root_run_id: str) -> int:
    return get_db().execute("SELECT COUNT(*) FROM agent_runs WHERE root_run_id = ?",
                            (root_run_id,)).fetchone()[0]


# --- notes -------------------------------------------------------------------

def _note(row: Any) -> dict[str, Any]:
    return {"id": row["id"], "agentId": row["agent_id"], "topic": row["topic"],
            "text": row["text"], "createdAt": row["created_at"], "updatedAt": row["updated_at"]}


@_locked
def list_notes(agent_id: str, topic: str | None = None) -> list[dict[str, Any]]:
    if topic:
        rows = get_db().execute(
            "SELECT * FROM agent_notes WHERE agent_id = ? AND lower(topic) = lower(?) "
            "ORDER BY updated_at DESC", (agent_id, topic.strip())).fetchall()
    else:
        rows = get_db().execute(
            "SELECT * FROM agent_notes WHERE agent_id = ? ORDER BY updated_at DESC",
            (agent_id,)).fetchall()
    return [_note(r) for r in rows]


@_locked
def write_note(agent_id: str, topic: str, text: str, *, mode: str = "replace") -> dict[str, Any]:
    """Keep something under a topic. `replace` overwrites that topic's note (a
    learner's current plan); `append` adds to it (a running log of what was found)."""
    topic = str(topic or "").strip()[:120]
    text = str(text or "").strip()
    if not topic or not text:
        raise AgentError("A note needs a topic and some text.")
    db = get_db()
    stamp = now_iso()
    existing = db.execute(
        "SELECT * FROM agent_notes WHERE agent_id = ? AND lower(topic) = lower(?) "
        "ORDER BY updated_at DESC LIMIT 1", (agent_id, topic)).fetchone()
    if existing is None:
        note_id = f"anote_{uuid.uuid4().hex[:12]}"
        db.execute("INSERT INTO agent_notes (id, agent_id, topic, text, created_at, updated_at) "
                   "VALUES (?, ?, ?, ?, ?, ?)", (note_id, agent_id, topic, text, stamp, stamp))
    else:
        note_id = existing["id"]
        body = f"{existing['text']}\n{text}" if mode == "append" else text
        db.execute("UPDATE agent_notes SET text = ?, updated_at = ? WHERE id = ?",
                   (body, stamp, note_id))
    return _note(db.execute("SELECT * FROM agent_notes WHERE id = ?", (note_id,)).fetchone())


@_locked
def update_note(note_id: str, *, topic: str | None = None, text: str | None = None) -> dict[str, Any]:
    db = get_db()
    row = db.execute("SELECT * FROM agent_notes WHERE id = ?", (note_id,)).fetchone()
    if row is None:
        raise AgentError("That note no longer exists.")
    db.execute("UPDATE agent_notes SET topic = ?, text = ?, updated_at = ? WHERE id = ?",
               ((topic or row["topic"]).strip(), (text if text is not None else row["text"]),
                now_iso(), note_id))
    return _note(db.execute("SELECT * FROM agent_notes WHERE id = ?", (note_id,)).fetchone())


@_locked
def delete_note(note_id: str) -> None:
    get_db().execute("DELETE FROM agent_notes WHERE id = ?", (note_id,))
