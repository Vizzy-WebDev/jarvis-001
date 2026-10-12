"""What a failure makes rest, and how widely. Shared by generation and embeddings so both
follow the error's own scope and recovery, never a status code."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import config as layer_config
from . import state
from .catalog import Connection
from .errors import ModelError


def daily_rest_s(connection: Connection, cfg: layer_config.Config) -> float:
    """Seconds until the provider's daily allowance resets. The reset hour (UTC) is quirk
    data; without it, the settings' default rest."""
    profile = cfg.quirk_profiles.get(connection.quirks or "")
    hour = (profile.wire if profile else {}).get("daily_reset_utc_hour")
    if not isinstance(hour, int) or not 0 <= hour < 24:
        return cfg.settings.daily_rest_s
    now = datetime.now(timezone.utc)
    reset = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if reset <= now:
        reset += timedelta(days=1)
    return (reset - now).total_seconds()


def note_failure(endpoint_id: str, connection: Connection, err: ModelError, cfg: layer_config.Config) -> None:
    s, conn = cfg.settings, connection.name
    if err.scope == "connection" and err.recovery == "user":
        state.refuse_connection(conn, str(err), s.refused_rest_s, kind=err.type)
        return
    if err.scope == "connection" and err.type == "unavailable":
        # The server could not be reached at all. The whole connection rests, exactly as when
        # it does not answer the model list — instead of each of its models being found dead
        # one at a time, by every call.
        state.mark_connection_down(conn, str(err), s.unreachable_rest_s)
        return
    if err.recovery == "refresh":
        state.mark_model_missing(endpoint_id, str(err))
    elif err.recovery == "reset":
        state.rest_endpoint(endpoint_id, daily_rest_s(connection, cfg), reason=str(err), kind="reset")
    elif err.type == "rate_limited":
        seconds = err.retry_after or s.default_rate_rest_s
        if state.siblings_rate_limited(conn, endpoint_id):
            state.rate_limit(conn, seconds)  # a second model limited too: it is the whole connection
        state.rest_endpoint(endpoint_id, seconds, reason=str(err))
    elif err.type in ("unavailable", "timeout"):
        state.record_failure(endpoint_id, str(err), threshold=s.breaker_threshold, base_s=s.breaker_base_s,
                             max_s=s.breaker_max_s)
