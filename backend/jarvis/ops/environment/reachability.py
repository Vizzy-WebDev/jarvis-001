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
    from ...models import selection

    state = selection.availability()
    provider_id, model_id, _ = selection.chosen()
    if state.state == "ok":
        return {"usable": [{"modelId": c.model.model_id, "connection": c.connection.label}
                           for c in selection.ready()[:5]],
                "blocked": [], "soonestRetryMs": None}
    if state.state == "none":
        return {"usable": [], "blocked": [], "soonestRetryMs": None}
    return {"usable": [], "blocked": [{"modelId": model_id, "reason": state.message}],
            "soonestRetryMs": None}


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
