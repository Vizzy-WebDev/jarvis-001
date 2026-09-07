"""Whether a model is currently usable — one store, one vocabulary.

Replaces three overlapping mechanisms in the Node implementation:

  * `models/health.js` — an in-memory breaker keyed on error KIND, lost on restart.
  * `models/router.js`'s AVAILABILITY_COOLDOWNS_MS — a persisted table keyed on
    availability STATE, with deliberately DIFFERENT values for the same failure
    (network 1min vs unreachable 10min; other 5min vs error 20min).
  * the availability blob stored inside `models.json` itself.

The consequence of having both tables was that the same failure produced a
different exclusion reason depending on how long the process had been running,
and a restart silently changed routing for reasons no user could perceive.

Two decisions worth keeping in view:

1. **Keyed on state, not kind.** State is the persisted vocabulary, so it is the
   one that survives a restart and the one a user-facing explanation is written
   in. `error_kind.py` remains the only kind->state mapping.

2. **Its own file, not models.json.** In the Node version, recording availability
   goes through `updateModel()`, which is read-whole-file / modify / write-whole-
   file with no locking — and "Check all models" runs three of those concurrently,
   so results were silently lost on exactly the screen that exists to show them.
   Availability is high-frequency machine-written state; models.json is low-
   frequency user-owned config. Separating them removes the race and means a user
   action can never be clobbered by a background health write.

The cooldown values below are taken from the reasoning already recorded in the
Node source rather than re-invented: `unreachable` keeps the longer of its two
competing values (a stale unreachable model is usually stale by hours, so the
short window was never protecting anything), and `error` keeps its own moderate
cooldown so an unclassified failure does not get the harshest penalty merely
because nothing else matched.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Iterable

from ..redact import redact_text
from ..store import read_json, write_json

FILE = "model-availability"

# Keyed on availability STATE. One table, one vocabulary.
COOLDOWNS_MS: dict[str, int] = {
    "quota": 30 * 60 * 1000,
    "auth": 6 * 60 * 60 * 1000,
    "no_access": 6 * 60 * 60 * 1000,
    # A provider-side overload clears in seconds to minutes, not hours.
    "busy": 2 * 60 * 1000,
    # A model that can never serve chat (wrong name, embedding-only, no tool
    # route) fails identically on every retry — there is nothing to wait for.
    "unsupported": 6 * 60 * 60 * 1000,
    "unreachable": 10 * 60 * 1000,
    # Deliberately moderate: an unrecognised failure should not inherit the
    # harshest cooldown simply because no classifier matched it.
    "error": 20 * 60 * 1000,
}

WORKING = "working"

_lock = threading.RLock()
_cache: dict[str, dict[str, Any]] | None = None


def _now_ms() -> int:
    return int(time.time() * 1000)


def _load() -> dict[str, dict[str, Any]]:
    global _cache
    with _lock:
        if _cache is None:
            stored = read_json(FILE, {}) or {}
            _cache = stored.get("models", {}) if isinstance(stored, dict) else {}
        return _cache


def _flush() -> None:
    """Write through. Called with the lock held."""
    write_json(FILE, {"models": _cache or {}})


def record(model_id: str, state: str, detail: str | None = None, technical: str | None = None) -> None:
    """Record the outcome of a real call against a model.

    `state` is the availability vocabulary (see COOLDOWNS_MS), or "working".
    `detail` is user-facing text; `technical` is the raw provider message, kept
    separately so a UI can show it behind a disclosure without it leaking into a
    plain-language message.

    Both are redacted HERE rather than by each caller. A provider error can
    quote the key that was sent, and this file outlives the process — so the
    guarantee has to hold for the caller who has not been written yet.
    """
    if not model_id or not state:
        return
    detail = redact_text(detail)
    technical = redact_text(technical)
    with _lock:
        models = _load()
        if state == WORKING:
            # A success clears the record entirely rather than storing a
            # "working" row — absence means usable, which keeps the file small
            # and makes "is anything wrong" a simple membership test.
            models.pop(model_id, None)
        else:
            models[model_id] = {
                "state": state,
                "detail": detail,
                "technical": technical,
                "since": _now_ms(),
            }
        _flush()


def status_of(model_id: str) -> dict[str, Any] | None:
    """The raw record for a model, or None if it is not currently benched."""
    return _load().get(model_id)


def is_eligible(model_id: str, now_ms: int | None = None) -> bool:
    """Whether this model may be offered to a caller right now."""
    record_ = _load().get(model_id)
    if not record_:
        return True
    now = now_ms if now_ms is not None else _now_ms()
    cooldown = COOLDOWNS_MS.get(record_.get("state", ""), COOLDOWNS_MS["error"])
    return now - int(record_.get("since", 0)) >= cooldown


def retry_after_ms(model_id: str, now_ms: int | None = None) -> int:
    """Milliseconds until this model becomes eligible again; 0 if it already is.

    This is what lets an "everything is rate-limited" message give a real
    estimate instead of an apology.
    """
    record_ = _load().get(model_id)
    if not record_:
        return 0
    now = now_ms if now_ms is not None else _now_ms()
    cooldown = COOLDOWNS_MS.get(record_.get("state", ""), COOLDOWNS_MS["error"])
    return max(0, cooldown - (now - int(record_.get("since", 0))))


def explain(model_ids: Iterable[str], now_ms: int | None = None) -> dict[str, Any]:
    """Why each ineligible model is excluded, and when the soonest one returns.

    One function so a caller cannot report a different reason than the one
    actually used to exclude — the Node version had the filter and the
    explanation in separate places, which is why capability exclusions could
    never be explained at all.
    """
    now = now_ms if now_ms is not None else _now_ms()
    excluded: dict[str, dict[str, Any]] = {}
    soonest: int | None = None
    for model_id in model_ids:
        if is_eligible(model_id, now):
            continue
        record_ = _load().get(model_id) or {}
        wait = retry_after_ms(model_id, now)
        excluded[model_id] = {
            "state": record_.get("state"),
            "detail": record_.get("detail"),
            "retryAfterMs": wait,
        }
        soonest = wait if soonest is None else min(soonest, wait)
    by_state: dict[str, int] = {}
    for info in excluded.values():
        key = str(info.get("state"))
        by_state[key] = by_state.get(key, 0) + 1
    return {"excluded": excluded, "counts": by_state, "soonestRetryMs": soonest}


def clear(model_id: str) -> None:
    """Forget a model's record — a user explicitly retrying, or a model edited."""
    with _lock:
        models = _load()
        if models.pop(model_id, None) is not None:
            _flush()


def reset_for_tests() -> None:
    global _cache
    with _lock:
        _cache = None
