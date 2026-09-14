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

#: Three preferences that used to live here are gone: `autoSelect`,
#: `manualModelId` and `voiceModelId`.
#:
#: All three were stored, served by this route, and read by absolutely nothing —
#: so anyone who set one had been running with a control that silently did not
#: work. The two pins are now role slots (`gateway/slots.py`), where they are
#: actually consulted, and existing values are adopted once on the way past
#: rather than dropped.
#:
#: `autoSelect` is deleted outright rather than moved. It expressed "use the
#: manual pick instead of ranking", which a pin either exists or does not
#: already says — a separate boolean for it could only ever disagree with the
#: thing it described.
DEFAULTS: dict[str, Any] = {
    "balance": "balanced",          # 'fast' | 'balanced' | 'quality'
    "clarifySensitivity": "balanced",  # 'more' | 'balanced' | 'less'
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
