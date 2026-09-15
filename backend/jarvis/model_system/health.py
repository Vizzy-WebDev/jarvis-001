"""Whether a model is currently usable, tracked per model (§13).

One row per model in `ai_health`, written on every real call — success or
failure — and read on every routing decision. Absence of a row (or a state of
`HEALTHY`) means nothing has ever gone wrong; a cooldown is not "this model is
broken forever," it is "wait this long before trying it again," and a single
transient failure must never mark a model permanently dead.

**Keyed on the MODEL, not the provider.** The same underlying model reached
through two different providers (your own key, and a reseller) is two
independent `ai_models` rows — see `ai/registry.py` — so a rate limit on one
route correctly never takes the other down with it.

**State drives the cooldown, not the raw error kind.** `error_kind.py`'s
richer taxonomy collapses to a small, stable health vocabulary here — several
`ErrorKind`s are genuinely the same fact for cooldown purposes (an
authentication failure and a permission failure are both "this credential
will not work until a person fixes it").
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Any

from ..db import get_db
from ..jscompat import now_iso
from ..redact import redact_text
from .errors import ErrorKind


class HealthState(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    AUTH_ERROR = "auth_error"
    COOLING_DOWN = "cooling_down"
    DISABLED = "disabled"


#: `ErrorKind` -> the health state it implies. A kind absent here (§12's
#: "errors that should generally not trigger blind retries" — an invalid
#: request, a refused parameter, content policy, context exceeded) is a fact
#: about the REQUEST, not the model, and never reaches this table at all —
#: see `errors.benches_the_model()`, which `ai/fallback.py` checks first.
STATE_FOR_KIND: dict[ErrorKind, HealthState] = {
    ErrorKind.RATE_LIMIT: HealthState.RATE_LIMITED,
    ErrorKind.AUTHENTICATION: HealthState.AUTH_ERROR,
    ErrorKind.PERMISSION_DENIED: HealthState.AUTH_ERROR,
    ErrorKind.MODEL_UNAVAILABLE: HealthState.UNAVAILABLE,
    ErrorKind.UNSUPPORTED_CAPABILITY: HealthState.UNAVAILABLE,
    ErrorKind.NETWORK: HealthState.DEGRADED,
    ErrorKind.TIMEOUT: HealthState.DEGRADED,
    ErrorKind.PROVIDER_UNAVAILABLE: HealthState.DEGRADED,
    ErrorKind.UNKNOWN: HealthState.COOLING_DOWN,
}

#: Cooldown per state. Values are deliberately not uniform: an auth failure
#: will not heal itself in twenty minutes the way an overloaded provider
#: will, and treating them the same either wastes a day retrying a dead key
#: or bench a healthy provider six times longer than it needed.
COOLDOWNS_MS: dict[HealthState, int] = {
    HealthState.RATE_LIMITED: 30 * 60 * 1000,
    HealthState.AUTH_ERROR: 6 * 60 * 60 * 1000,
    HealthState.UNAVAILABLE: 6 * 60 * 60 * 1000,
    HealthState.DEGRADED: 2 * 60 * 1000,
    HealthState.COOLING_DOWN: 20 * 60 * 1000,
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _row(model_id: str) -> Any:
    return get_db().execute("SELECT * FROM ai_health WHERE model_id = ?", (model_id,)).fetchone()


def record_success(model_id: str) -> None:
    db = get_db()
    now = now_iso()
    db.execute(
        "INSERT INTO ai_health (model_id, state, detail, technical, since, failure_count, "
        "last_success) VALUES (?, 'healthy', NULL, NULL, ?, 0, ?) "
        "ON CONFLICT(model_id) DO UPDATE SET state = 'healthy', detail = NULL, technical = NULL, "
        "since = excluded.since, failure_count = 0, last_success = excluded.last_success",
        (model_id, now, now),
    )


def record_failure(model_id: str, kind: ErrorKind, *, detail: str | None = None,
                   technical: str | None = None) -> None:
    """Record a failure that is evidence about the MODEL. A caller should
    check `errors.benches_the_model(kind)` first — see `ai/fallback.py`,
    which is the one place that decides whether to call this at all."""
    state = STATE_FOR_KIND.get(kind, HealthState.COOLING_DOWN)
    detail = redact_text(detail)
    technical = redact_text(technical)
    db = get_db()
    now = now_iso()
    existing = _row(model_id)
    failure_count = (existing["failure_count"] if existing is not None else 0) + 1
    db.execute(
        "INSERT INTO ai_health (model_id, state, detail, technical, since, failure_count, "
        "last_failure) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(model_id) DO UPDATE SET state = excluded.state, detail = excluded.detail, "
        "technical = excluded.technical, since = excluded.since, "
        "failure_count = excluded.failure_count, last_failure = excluded.last_failure",
        (model_id, state.value, detail, technical, now, failure_count, now),
    )


def status_of(model_id: str) -> dict[str, Any] | None:
    row = _row(model_id)
    if row is None:
        return None
    return {
        "state": row["state"], "detail": row["detail"], "technical": row["technical"],
        "since": row["since"], "failureCount": row["failure_count"],
        "lastSuccess": row["last_success"], "lastFailure": row["last_failure"],
    }


def _since_ms(iso: str) -> int:
    import datetime as _dt
    try:
        return int(_dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S.%fZ")
                   .replace(tzinfo=_dt.timezone.utc).timestamp() * 1000)
    except ValueError:
        return 0


def is_eligible(model_id: str) -> bool:
    """Whether this model may be offered right now. `HEALTHY` and `DISABLED`
    are not in `COOLDOWNS_MS` — a caller checks `enabled` separately (that is
    a user choice, not a health fact) — so anything else always resolves
    through the table."""
    row = _row(model_id)
    if row is None or row["state"] in ("healthy",):
        return True
    state = HealthState(row["state"])
    cooldown = COOLDOWNS_MS.get(state)
    if cooldown is None:
        return True
    return _now_ms() - _since_ms(row["since"]) >= cooldown


def retry_after_ms(model_id: str) -> int:
    row = _row(model_id)
    if row is None:
        return 0
    state = HealthState(row["state"])
    cooldown = COOLDOWNS_MS.get(state)
    if cooldown is None:
        return 0
    return max(0, cooldown - (_now_ms() - _since_ms(row["since"])))


def clear(model_id: str) -> None:
    get_db().execute("DELETE FROM ai_health WHERE model_id = ?", (model_id,))
