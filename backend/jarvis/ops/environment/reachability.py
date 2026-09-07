"""Can Jarvis actually reach the things it would need for this?

A read across subsystems that already track their own answer — never a new
probe. Probing here would mean a second, differently-timed opinion about the
same fact, and the two would disagree exactly when it mattered.
"""

from __future__ import annotations

from typing import Any


def models() -> dict[str, Any]:
    from ...gateway import availability, registry
    from ...gateway.registry import is_ready

    entries = registry.list_models()
    usable, blocked = [], []
    for entry in entries:
        if not entry.get("enabled", True):
            continue
        if not is_ready(entry):
            blocked.append({"modelId": entry["id"], "reason": "needs a key"})
            continue
        if not availability.is_eligible(entry["id"]):
            record = availability.status_of(entry["id"]) or {}
            blocked.append({"modelId": entry["id"],
                            "reason": record.get("detail") or record.get("state") or "unavailable",
                            "retryInMs": availability.retry_after_ms(entry["id"])})
            continue
        usable.append(entry["id"])

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
