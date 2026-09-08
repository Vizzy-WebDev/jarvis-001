"""Open a section of the app for the user, by voice or by asking.

The navigation itself happens in the browser: this returns where to go and the
interface acts on it. That split is why this tool knows nothing about routing,
and why a caller with no interface at all (a scheduled run) gets a harmless
result rather than an error.
"""

from __future__ import annotations

from ..capabilities import CapabilitySpec, Risk

#: Must stay in step with the interface's own section registry
#: (`frontend/lib/nav.ts`), which drives the drawer, the router and this. A name
#: here that is missing there navigates nowhere, and that drift has happened
#: before — three real sections were absent from this list for a while.
SECTIONS = {
    "home": "the assistant",
    "notifications": "Notifications",
    "chat-history": "Chat History",
    "models": "Model Settings",
    "skills": "Skills",
    "app-control": "App Control",
    "tasks": "Scheduled Tasks",
    "jobs": "Background Jobs",
    "briefing": "Morning Briefing",
    "profile": "Profile & Goals",
    "memory": "Memory",
    "improvement": "Self-Improvement",
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
        "Open a section of the Jarvis app for the user — Model Settings, Skills, App Control, "
        "Scheduled Tasks, Background Jobs, Morning Briefing, Notifications, Chat History, "
        "Memory, Self-Improvement, or Profile & Goals. Use it when they say “open…”, "
        "“show me…” or “go to…” one of those. Planning and shared content are not sections: "
        "they happen in the conversation, so never try to open a page for them."),
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
