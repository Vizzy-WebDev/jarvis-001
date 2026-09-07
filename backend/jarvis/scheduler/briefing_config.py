"""The briefing's saved configuration — deliberately dependency-free.

Split from the composer so a tool can read and write it without dragging in the
model layer. In the Node original that separation prevented a real deadlock (a
tool reaching back through the composer into the loader that was still importing
it); here it is simply the right shape, and keeping it means the same mistake
cannot be made later.

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
