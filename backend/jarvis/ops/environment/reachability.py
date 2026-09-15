"""Can Jarvis actually reach the things it would need for this?

A read across subsystems that already track their own answer — never a new
probe. Probing here would mean a second, differently-timed opinion about the
same fact, and the two would disagree exactly when it mattered.
"""

from __future__ import annotations

from typing import Any


def models() -> dict[str, Any]:
    from ...model_system import health
    from ...model_system.credentials import CredentialStatus
    from ...model_system.registry import list_models

    usable, blocked = [], []
    for model in list_models():
        if not model.enabled:
            continue
        if model.provider.credential_status is not CredentialStatus.CONFIGURED:
            blocked.append({"modelId": model.id, "reason": "needs a key"})
            continue
        if not health.is_eligible(model.id):
            record = health.status_of(model.id) or {}
            blocked.append({"modelId": model.id,
                            "reason": record.get("detail") or record.get("state") or "unavailable",
                            "retryInMs": health.retry_after_ms(model.id)})
            continue
        usable.append(model.id)

    soonest = [b["retryInMs"] for b in blocked if b.get("retryInMs")]
    return {"usable": usable, "blocked": blocked,
            "soonestRetryMs": min(soonest) if soonest else None}


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
