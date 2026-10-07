"""Open a section of the app for the user, by voice or by asking.

The navigation itself happens in the browser: this returns where to go and the
interface acts on it. That split is why this tool knows nothing about routing,
and why a caller with no interface at all (a scheduled run) gets a harmless
result rather than an error.
"""

from __future__ import annotations

from ..capabilities import CapabilitySpec, Risk

#: Must stay in step with the interface's own page registry
#: (`frontend/lib/nav.ts`), which drives the menu, the router and this. Every
#: name here is a page, a page's tab (`settings/voice`), or one of the
#: interface's aliases for a tab (`memory` is Knowledge → Memory) — a name it
#: does not know navigates nowhere, and that drift has happened before.
#: `tests/test_tools.py` checks every one against nav.ts.
SECTIONS = {
    "home": "Home",
    "notifications": "Notifications",
    "chat-history": "Chat History",
    "artifacts": "Artifacts",
    "content": "Content Management",
    "abilities": "Abilities",
    "agents": "Specialists",
    "jobs": "Background Jobs",
    "routines": "Routines",
    "tasks": "Scheduling",
    "briefing": "Morning Briefing",
    "capabilities": "Capabilities",
    "capabilities/connectors": "Connectors",
    "skills": "Skills",
    "you": "You",
    "profile": "your Profile",
    "you/goals": "Goals",
    "knowledge": "Knowledge",
    "memory": "Memory",
    "knowledge/graph": "the Knowledge Graph",
    "improvement": "Self-Improvement",
    "settings": "Settings",
    "models": "Model settings",
    "settings/voice": "Voice settings",
    "settings/appearance": "Appearance settings",
    "settings/quiet-hours": "Quiet Hours",
    "settings/presence": "Presence and sound settings",
    "settings/displays": "Display settings",
    "settings/jobs": "Background job settings",
}


def _run(section: str = "") -> dict:
    name = (section or "").strip().lower()
    if name not in SECTIONS:
        return {"ok": False,
                "error": f"There is no “{section}” section.",
                "sections": sorted(SECTIONS)}
    return {"ok": True, "speak": f"Opening {SECTIONS[name]}.",
            "ui_action": {"type": "navigate", "section": name}}


SPEC = CapabilitySpec(
    id="builtin.open_section",
    name="open_section",
    description=(
        "Open a page of the Jarvis app for the user — Notifications, Chat History, Artifacts, "
        "Content Management (reviewing, scheduling and publishing their content), Specialists, "
        "Background Jobs, Scheduling, Morning Briefing, Connectors, Skills, their Profile, Goals, "
        "Memory, the Knowledge Graph, Self-Improvement, or Settings (models, voice, appearance, "
        "quiet hours, presence and sound, displays, background jobs). Use it when they say “open…”, "
        "“show me…” or “go to…” one of those. Planning, and looking into something they "
        "shared with you, are not sections: they happen in the conversation, so never try "
        "to open a page for them."),
    input_schema={"type": "object", "properties": {
        "section": {"type": "string", "enum": sorted(SECTIONS),
                    "description": "Which section to open."}},
        "required": ["section"]},
    risk=Risk.LOW,
    handler=_run,
    timeout_s=5.0,
    # `core` so it is declared in ordinary conversation; `meta` so it stays out
    # of a scheduled task's or a briefing's picker — opening a page for nobody
    # to look at is not work anybody would schedule.
    tags=frozenset({"core", "meta"}),
)
