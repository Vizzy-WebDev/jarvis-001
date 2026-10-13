"""Can Jarvis actually reach the things it would need for this?

A read across subsystems that already track their own answer — never a new
probe. Probing here would mean a second, differently-timed opinion about the
same fact, and the two would disagree exactly when it mattered.
"""

from __future__ import annotations

from typing import Any


def models() -> dict[str, Any]:
    """Whether the selected model could answer right now, and if not, why.

    Read from the selection and what is stored about it — no provider is called.
    A model that is merely selected is not reported usable: 'usable' means the
    selection resolves to a connection with what it needs."""
    from ...models import settings

    state = settings.availability()
    try:
        _, model_id = settings.chosen()
    except Exception:  # noqa: BLE001 - a broken config is reported through availability
        model_id = None
    if state.state == "ok":
        return {"usable": [{"modelId": e.model_id, "connection": e.connection} for e in settings.ready(5)],
                "blocked": [], "soonestRetryMs": None}
    if state.state == "none":
        return {"usable": [], "blocked": [], "soonestRetryMs": None}
    return {"usable": [], "blocked": [{"modelId": model_id, "reason": state.message}],
            "soonestRetryMs": None}


def voice() -> dict[str, Any]:
    """Speech in and out. The browser's own voice needs no server component and
    is therefore always available — which is why "no key" never means "cannot
    speak"."""
    from ...assembly import get_wake_detector
    from ...prefs import get_prefs

    prefs = get_prefs()
    # The detector's own answer. This used to say "available" unconditionally,
    # while the model was not even on the machine.
    wake = get_wake_detector().status()
    return {"speechOut": {"provider": prefs.get("ttsProvider") or "browser",
                          "alwaysAvailable": True},
            "wakeWord": {"available": bool(wake["available"]), "note": wake["note"]}}


def snapshot() -> dict[str, Any]:
    return {"models": models(), "voice": voice()}
