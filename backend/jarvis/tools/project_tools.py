"""Talking a project through: the notepad, the decisions, and the handoff.

Five tools, and none of them triggers the next. That is deliberate and load
bearing: an earlier design researched an idea the moment it was mentioned and
wrote the plan the instant its question queue emptied, which is how a
conversation turns into a conveyor belt nobody asked to be on.
"""

from __future__ import annotations

from typing import Any

from ..capabilities import CapabilitySpec, Risk
from ..projects.assistants import ASSISTANTS

ASSISTANT_IDS = [a["id"] for a in ASSISTANTS]
_TARGET_HELP = ("Which AI will build this: claude-code (a terminal coding agent), chat "
                "(ChatGPT or Claude in a chat window), editor (Cursor, Windsurf, Copilot), "
                "builder (Lovable, v0, Bolt, Replit), or custom for anything else.")


def _target(target: str | None, target_name: str | None) -> dict[str, Any] | None:
    if not target:
        return None
    if target not in ASSISTANT_IDS:
        return None
    entry: dict[str, Any] = {"id": target}
    if target == "custom" and (target_name or "").strip():
        entry["name"] = target_name.strip()
    return entry


def _resolve(project_id: str | None) -> dict[str, Any] | None:
    from ..projects import store
    from ..session import get_active_session_id

    if project_id:
        found = store.get_project(project_id)
        if found:
            return found
    return store.latest_in_session(get_active_session_id())


_NOTHING = {"ok": False,
            "error": "There's no project on the go here. Start one first if that's what "
                     "they meant."}


def _start(idea: str = "", target: str = "", target_name: str = "") -> dict[str, Any]:
    from ..projects.engine import start_project
    from ..session import get_active_session_id

    text = (idea or "").strip()
    if not text:
        return {"ok": False, "error": "I didn't catch what they wanted to build."}
    project = start_project(idea=text, session_id=get_active_session_id(),
                            target=_target(target, target_name))
    return {"ok": True, "projectId": project["id"], "title": project["title"],
            "spoken_hint": ("Just carry on talking about the idea normally. Nothing has been "
                            "researched or written — don't announce a process, and don't "
                            "start asking a list of questions.")}


def _note_decision(decision: str = "", project_id: str = "") -> dict[str, Any]:
    from ..projects.engine import note_decision

    text = (decision or "").strip()
    if not text:
        return {"ok": False, "error": "There was no decision to record."}
    project = _resolve(project_id)
    if project is None:
        return _NOTHING
    note_decision(project["id"], text)
    return {"ok": True, "projectId": project["id"],
            "spoken_hint": "Acknowledge it in passing at most. Don't read it back as a list."}


def _research(project_id: str = "") -> dict[str, Any]:
    from ..projects.engine import research_project

    project = _resolve(project_id)
    if project is None:
        return _NOTHING
    research_project(project["id"])
    return {"ok": True, "projectId": project["id"],
            "spoken_hint": ("Say you're looking into it. What you find arrives in the "
                            "conversation shortly — don't invent findings now.")}


def _write_plan(project_id: str = "") -> dict[str, Any]:
    from ..projects.engine import write_plan

    project = _resolve(project_id)
    if project is None:
        return _NOTHING
    write_plan(project["id"])
    return {"ok": True, "projectId": project["id"],
            "spoken_hint": ("Say you're writing it up. The plan appears in the conversation "
                            "when it's ready — give the gist then, never the whole document.")}


def _write_prompts(project_id: str = "", target: str = "",
                   target_name: str = "") -> dict[str, Any]:
    from ..projects.engine import write_prompts

    project = _resolve(project_id)
    if project is None:
        return _NOTHING
    if not project.get("plan"):
        return {"ok": False, "projectId": project["id"],
                "error": "That project doesn't have a written plan yet — write the plan first."}

    chosen = _target(target, target_name) or project.get("target")
    if not chosen:
        return {"ok": False, "projectId": project["id"],
                "error": "Ask them which AI is going to build this before writing the prompt — "
                         "the same plan needs a different prompt for each one."}
    write_prompts(project["id"], chosen)
    return {"ok": True, "projectId": project["id"],
            "spoken_hint": ("Say it's coming. The prompt appears in the conversation when "
                            "ready — never read a build prompt out loud.")}


SPECS = [
    CapabilitySpec(
        id="builtin.start_project", name="start_project",
        description=("Open a project notepad for something the user wants to build — a site, "
                     "an app, a business, anything. Use it when they first describe an idea, "
                     "however rough. This ONLY creates the notepad: it does not research "
                     "anything or ask questions on its own. Afterwards just talk about the "
                     "idea normally, and use note_project_decision as things get settled."),
        input_schema={"type": "object", "properties": {
            "idea": {"type": "string",
                     "description": ("Their idea, in their own words, as fully as they "
                                     "described it. Do not summarise it down.")},
            "target": {"type": "string", "enum": ASSISTANT_IDS,
                       "description": _TARGET_HELP + " Only if they already said."},
            "target_name": {"type": "string",
                            "description": 'If target is "custom", the tool they named.'}},
            "required": ["idea"]},
        risk=Risk.LOW, handler=_start, timeout_s=10.0, tags=frozenset({"meta"}),
    ),
    CapabilitySpec(
        id="builtin.note_project_decision", name="note_project_decision",
        description=("Record something genuinely SETTLED about a project being planned — who "
                     "it is for, what the first version must do, a budget, a preference. Use "
                     "it as decisions come up naturally, not as a batch at the end. Never for "
                     "something still being weighed, or a question not yet answered."),
        input_schema={"type": "object", "properties": {
            "decision": {"type": "string",
                         "description": ('The decision, plainly and on its own — e.g. "no '
                                         'user accounts in the first version".')},
            "project_id": {"type": "string",
                           "description": ("Which project. Leave out if there is genuinely "
                                           "only one being discussed.")}},
            "required": ["decision"]},
        risk=Risk.LOW, handler=_note_decision, timeout_s=10.0, tags=frozenset({"meta"}),
    ),
    CapabilitySpec(
        id="builtin.research_project", name="research_project",
        description=("Look into what building a project idea actually involves — practical "
                     "steps, typical costs, common mistakes. ONLY when they clearly ask you "
                     "to look into it. Never automatically because an idea was mentioned."),
        input_schema={"type": "object", "properties": {
            "project_id": {"type": "string",
                           "description": "Which project. Leave out if there is only one."}},
            "required": []},
        risk=Risk.LOW, handler=_research, timeout_s=15.0, tags=frozenset({"meta"}),
    ),
    CapabilitySpec(
        id="builtin.write_project_plan", name="write_project_plan",
        description=("Write the first-version plan document for a project being discussed. "
                     "ONLY when they clearly ask for it to be written. Never automatically "
                     "because discussion or research happened. Research is not required "
                     "first — write from whatever is known if they want to skip it."),
        input_schema={"type": "object", "properties": {
            "project_id": {"type": "string",
                           "description": "Which project. Leave out if there is only one."}},
            "required": []},
        risk=Risk.LOW, handler=_write_plan, timeout_s=15.0, tags=frozenset({"meta"}),
    ),
    CapabilitySpec(
        id="builtin.write_build_prompts", name="write_build_prompts",
        description=("Write the ready-to-paste build prompt(s) for a project that already has "
                     "a plan. May produce one or several in sequence. ONLY when they clearly "
                     "ask for it — never automatically because the plan finished."),
        input_schema={"type": "object", "properties": {
            "project_id": {"type": "string",
                           "description": "Which project. Leave out if there is only one."},
            "target": {"type": "string", "enum": ASSISTANT_IDS, "description": _TARGET_HELP},
            "target_name": {"type": "string",
                            "description": 'If target is "custom", the tool they named.'}},
            "required": []},
        risk=Risk.LOW, handler=_write_prompts, timeout_s=15.0, tags=frozenset({"meta"}),
    ),
]
