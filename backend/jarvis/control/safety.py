"""What is off-limits when Jarvis is driving, and how long captures are kept.

Merge-over-defaults, so a field added later is automatically compatible with
whatever is already saved on disk — the same pattern `prefs.py` uses.

The defaults are not empty, deliberately: banking sites, the well-known password
managers and Windows' own security prompts are blocked from the first run rather
than left for someone to discover the hard way. A list is REPLACED rather than
merged when the user has saved their own, so removing a default entry actually
removes it instead of having it quietly reappear.
"""

from __future__ import annotations

from typing import Any

from ..store import read_json, write_json

FILE = "safety"

DEFAULTS: dict[str, Any] = {
    # Jarvis explains its plan once, the user approves, then it works through
    # the steps — still stopping for anything the guard calls risky.
    "autonomy": "confirmPlan",
    "blockedWindowPatterns": [
        "bank", "banking", "1password", "bitwarden", "lastpass", "keepass",
        "windows security", "user account control",
        # Jarvis's own server window carries this phrase; a control session
        # clicking into it and sending Ctrl+C would stop the server running it.
        "keep this window open",
    ],
    # consent.exe hosts the Windows UAC prompt.
    "blockedProcesses": ["1password", "bitwarden", "keepassxc", "lastpass", "consent"],
    "blockedUrlPatterns": ["chase.com", "bankofamerica.com", "wellsfargo.com",
                           "paypal.com/signin"],
    "screenshotRetention": {"maxCount": 50, "maxAgeHours": 24},
    # Much lower, because a video file is much bigger.
    "recordingRetention": {"maxCount": 10, "maxAgeHours": 24},
}

_LISTS = ("blockedWindowPatterns", "blockedProcesses", "blockedUrlPatterns")
_NESTED = ("screenshotRetention", "recordingRetention")


def get_safety_config() -> dict[str, Any]:
    saved = read_json(FILE, {}) or {}
    config: dict[str, Any] = {**DEFAULTS, **saved}
    for key in _LISTS:
        config[key] = saved[key] if isinstance(saved.get(key), list) else DEFAULTS[key]
    for key in _NESTED:
        config[key] = {**DEFAULTS[key], **(saved.get(key) or {})}
    return config


def set_safety_config(partial: dict[str, Any]) -> dict[str, Any]:
    updated = {**get_safety_config(), **(partial or {})}
    write_json(FILE, updated)
    return updated
