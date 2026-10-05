"""App-wide preferences that aren't a single model's business.

Follows a "merge over defaults" pattern, so adding a preference later stays
backward-compatible with whatever is already saved on disk.

Defaults include ones that are deliberately NOT opt-in dials. Changing a default here
silently changes behaviour for anyone who has never opened the corresponding screen, which
is why each is annotated with its reasoning.
"""

from __future__ import annotations

from typing import Any

from .store import read_json, write_json

FILE = "prefs"

DEFAULTS: dict[str, Any] = {
    "clarifySensitivity": "balanced",  # 'more' | 'balanced' | 'less'
    # Last-resort fallback only — in practice the client always sends its own
    # choice. None rather than a provider name: no provider is guaranteed
    # configured, and the TTS seam handles None cleanly.
    "ttsProvider": None,
    # The person's own choice of model — the connection, the provider's own model
    # id, and (only where that model reports levels) a reasoning effort. None until
    # they choose. Nothing but them ever changes these: a selection that stops
    # being runnable is reported as such, never swapped for another.
    "selectedProviderId": None,
    "selectedModelId": None,
    "selectedEffort": None,
    # True when they chose Auto instead of naming a model: Jarvis then picks per
    # turn from the models that are set up (see models/auto.py). Choosing a model
    # by name turns it off; nothing else does.
    "selectedAuto": False,
    # How much work Jarvis does AROUND a model on a turn, not which model: 'fast'
    # caps the tool-use rounds and skips the optional extra checks, 'quality'
    # forces the answer check on. Applies to every model alike — including ones
    # with no effort control at all, which is why it is not the same thing.
    "balance": "balanced",  # 'fast' | 'balanced' | 'quality'
    # How much Memory saves without asking. 'ask' is approval-first, and is the default so nothing changes
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


def forget(*names: str) -> dict[str, Any]:
    """Remove preferences that no longer exist from the stored file.

    `set_prefs` merges, so it can change a value but never drop one: a key
    written by an older build survives every subsequent write and goes on being
    served by the preferences route forever. That is how a setting nothing reads
    stays visible to a screen that might still offer it.

    Only touches keys actually present, so this is a no-op on a fresh install
    and writes nothing when there is nothing to remove.
    """
    saved = read_json(FILE, {}) or {}
    doomed = [name for name in names if name in saved]
    if not doomed:
        return get_prefs()
    for name in doomed:
        saved.pop(name, None)
    write_json(FILE, saved)
    return get_prefs()
