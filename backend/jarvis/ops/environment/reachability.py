"""Can Jarvis actually reach the things it would need for this?

A read across subsystems that already track their own answer — never a new
probe. Probing here would mean a second, differently-timed opinion about the
same fact, and the two would disagree exactly when it mattered.
"""

from __future__ import annotations

from typing import Any


def models() -> dict[str, Any]:
    """Which AI models could answer right now. None, while there is no model
    system."""
    return {"usable": [], "blocked": [], "soonestRetryMs": None}


def voice() -> dict[str, Any]:
    """Speech in and out. The browser's own voice needs no server component and
    is therefore always available — which is why "no key" never means "cannot
    speak"."""
    from ...prefs import get_prefs

    prefs = get_prefs()
    return {"speechOut": {"provider": prefs.get("ttsProvider") or "browser",
                          "alwaysAvailable": True},
            "wakeWord": {"available": True, "note": "runs locally; no network needed"}}


def snapshot() -> dict[str, Any]:
    return {"models": models(), "voice": voice()}
