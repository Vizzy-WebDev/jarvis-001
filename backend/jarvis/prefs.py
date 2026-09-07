"""App-wide preferences that aren't a single model's business.

A port of server/prefs.js. Follows the same "merge over defaults" pattern, so
adding a preference later stays backward-compatible with whatever is already
saved on disk — and, during the migration, so this reads a prefs.json the Node
app wrote and vice versa.

The defaults are reproduced exactly, including the ones that are deliberately
NOT opt-in dials. Changing a default here silently changes behaviour for anyone
who has never opened the corresponding screen, which is why each is annotated
with the reasoning the original recorded.
"""

from __future__ import annotations

from typing import Any

from .store import read_json, write_json

FILE = "prefs"

DEFAULTS: dict[str, Any] = {
    "autoSelect": True,
    "balance": "balanced",          # 'fast' | 'balanced' | 'quality'
    "manualModelId": None,          # used when autoSelect is off; leads the candidate list either way
    "clarifySensitivity": "balanced",  # 'more' | 'balanced' | 'less'
    # One model pinned specifically for voice turns, outranking manualModelId
    # for voice so a spoken conversation never lands on whatever wins the
    # router's speed/cost tie-break.
    "voiceModelId": None,
    # Last-resort fallback only — in practice the client always sends its own
    # choice. None rather than a provider name: no provider is guaranteed
    # configured, and the TTS seam handles None cleanly.
    "ttsProvider": None,
    # How much Memory saves without asking. 'ask' reproduces the original
    # approval-first behaviour exactly, and is the default so nothing changes
    # for anyone who has not turned the dial up themselves. A candidate that
    # conflicts with an existing memory always requires approval regardless —
    # that floor lives in the memory policy, never here.
    "memoryTrust": "ask",           # 'ask' | 'balanced' | 'auto'
    # A starting default, not a hardcoded ceiling — the right number depends on
    # real usage and how heavy a job turns out to be.
    "maxBackgroundJobs": 2,
    "improvementEnabled": True,
    # 'balanced' rather than 'ask' (unlike memoryTrust) by explicit choice:
    # small, well-evidenced behaviour fixes are meant to just happen and be
    # reported afterward. The hard floor that no trust level overrides lives in
    # the improvement policy.
    "improvementTrust": "balanced",  # 'ask' | 'balanced' | 'auto'
    "improvementResearch": "weekly",  # 'off' | 'weekly'
    # The one pref here that is NOT an opt-in dial: a brand-new install should
    # never get proactive contact overnight before the user has even seen the
    # setting. A window wrapping past midnight is handled by the quiet-hours
    # module, not by this shape.
    "quietHours": {"enabled": True, "start": "23:00", "end": "08:00"},
    # Whether a consequential CHAT answer gets a semantic check ("does this
    # genuinely answer what was asked") after it has been given. Off by
    # default: it is a real model call per checked answer, on a roster that is
    # routinely rate limited, and chat is the most frequent surface there is.
    # Built because the owner asked for it to exist and to be theirs to switch
    # on — not for it to start spending on their behalf.
    "verifyChatAnswers": False,
}


def get_prefs() -> dict[str, Any]:
    saved = read_json(FILE, {}) or {}
    return {**DEFAULTS, **saved}


def set_prefs(partial: dict[str, Any]) -> dict[str, Any]:
    nxt = {**get_prefs(), **(partial or {})}
    write_json(FILE, nxt)
    return nxt
