"""The briefing's saved configuration — deliberately dependency-free.

Split from the composer so a tool can read and write it without dragging in the
model layer. A tool reaching back through the composer into the loader that was still
importing it would deadlock, so keeping the split means that mistake cannot be made.

Weather and headlines are fixed, always-available abilities, NOT a user-managed
list of "sources" — that shape is what once let Jarvis's own built-in abilities
be offered through an "add a source" picker as though they were installable
skills.
"""

from __future__ import annotations

from typing import Any

from ..store import read_json, write_json

FILE = "briefing"

DEFAULT_CONFIG: dict[str, Any] = {
    "sections": {
        "greeting": True,
        "dateTime": True,
        "tasks": True,
        "goals": True,
        "focus": True,
        "custom": False,
    },
    "customText": "",
    #: Empty means weather is skipped. Set conversationally — a briefing cannot
    #: say anything about weather without knowing where the user is.
    "weatherPlace": "",
    "headlines": False,
    #: Connector ids the person explicitly picked to contribute here. Empty by
    #: default: nothing is automatic. Ids, never tool names — a connector's tool
    #: list changes when it is reconnected, so a saved name would go quietly
    #: stale while a saved id resolves freshly on every run (and a since-removed
    #: connector simply contributes nothing rather than breaking the briefing).
    #:
    #: This replaces what were once fixed, permanently-disabled calendar and
    #: email stubs, which assumed reaching a real calendar meant building a
    #: separate sign-in flow. A general connector system already exists; this is
    #: that mechanism rather than a special case for two named services.
    "connectors": [],
}


def get_config() -> dict[str, Any]:
    saved = read_json(FILE, {})
    if not isinstance(saved, dict):
        return dict(DEFAULT_CONFIG)
    config = {**DEFAULT_CONFIG, **saved}
    config["sections"] = {**DEFAULT_CONFIG["sections"], **(saved.get("sections") or {})}
    return config


def set_config(patch: dict[str, Any]) -> dict[str, Any]:
    config = get_config()
    sections = {**config["sections"], **(patch.get("sections") or {})}
    config.update({k: v for k, v in patch.items() if k != "sections"})
    config["sections"] = sections
    write_json(FILE, config)
    return config
