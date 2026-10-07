"""The state file — what the layer has learned, kept apart from what the person
configured: the last discovery per connection, probe results, latency, endpoint
health (the circuit breaker), connection rests (rate limits, unreachable, a refused
key or account), and this month's spend.

Never written to config. Held in memory and written to `data/models_state.json`
at most once a second (and at exit); losing the last second of latency samples
to a crash costs nothing. Every refusal and rest is written at once, though.

A file that can't be read as state is never silently replaced by an empty one: it is
copied to `models_state.json.bak` first, with a warning in the log and a notice.
"""

from __future__ import annotations

import atexit
import logging
import shutil
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from .. import store
from .catalog import Pricing
from .prepared import Discovered

logger = logging.getLogger(__name__)

FILE = "models_state"
#: 2: capability vocabulary v3 (cached listings are converted on load), and the context
#: windows learned from refusals are kept as dated `observed` values.
VERSION = 2
_FLUSH_EVERY_S = 1.0

#: The clock, named so a test can move time instead of waiting for it.
now: Callable[[], float] = time.time

_lock = threading.RLock()
_data: dict[str, Any] | None = None
_where: str | None = None
_dirty = False
_last_flush = 0.0
#: Bumped whenever what the catalog is built from (listings, probes) changes.
_catalog_version = 0


def catalog_version() -> tuple[str | None, int]:
    with _lock:
        _state()
        return _where, _catalog_version


def _catalog_changed() -> None:
    global _catalog_version
    _catalog_version += 1


def _empty() -> dict[str, Any]:
    return {"version": VERSION, "discovery": {}, "probes": {}, "latency": {}, "health": {},
            "connections": {}, "spend": {"month": None, "usd": 0.0}, "notices": [], "observed": {}}


def _convert_caps(caps: dict[str, Any]) -> dict[str, Any]:
    """Capabilities cached under vocabulary v2, in v3's terms. A one-time conversion of what
    a server listed - nothing else reads the old names."""
    out = dict(caps)
    controlled = out.pop("reasoning_control", None)
    levels, default = out.pop("effort_levels", None), out.pop("effort_default", None)
    if controlled and isinstance(levels, list) and levels:
        out["reasoning_levels"] = levels
        if default in levels:
            out["reasoning_default"] = default
        if "none" in levels:
            out["reasoning_can_disable"] = True
    if out.pop("embeddings", None):
        out["kind"] = "embedding"
    if out.get("text_in") is False:
        out.pop("text_in")
        out.setdefault("kind", "other")
    return out


def _parse_iso(text: Any) -> float:
    try:
        return datetime.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return now()


def _convert(loaded: dict[str, Any]) -> dict[str, Any]:
    for entry in (loaded.get("discovery") or {}).values():
        for row in (entry or {}).get("models") or []:
            if isinstance(row, dict) and isinstance(row.get("capabilities"), dict):
                row["capabilities"] = _convert_caps(row["capabilities"])
    for probe in (loaded.get("probes") or {}).values():
        if isinstance(probe, dict) and isinstance(probe.get("capabilities"), dict):
            probe["capabilities"] = _convert_caps(probe["capabilities"])
    old = loaded.pop("learned", None) or {}
    observed = loaded.setdefault("observed", {})
    for eid, row in old.items():
        if isinstance(row, dict) and isinstance(row.get("max_context_tokens"), int):
            observed.setdefault(eid, {"ts": _parse_iso(row.get("at")), "at": row.get("at"),
                                      "max_context_tokens": row["max_context_tokens"]})
    loaded["version"] = VERSION
    return loaded


def _read_state() -> dict[str, Any]:
    target = store.data_dir() / (FILE + ".json")
    loaded = store.read_json(FILE, None)
    if loaded is None and not target.exists():
        return _empty()
    if not isinstance(loaded, dict):
        backup = target.with_name(target.name + ".bak")
        try:
            shutil.copy2(target, backup)
            where = "A copy was kept as " + backup.name + "."
        except OSError:
            where = "A copy of it could not be kept."
        logger.warning("%s couldn't be read as model state; starting from empty. %s", target.name, where)
        base = _empty()
        base["notices"].append({"at": _iso(), "text": "Jarvis's saved model status file was damaged, so it "
                                                      "started fresh and will re-check your connections. " + where})
        return base
    if loaded.get("version") != VERSION:
        loaded = _convert(loaded)
    base = _empty()
    for key in base:
        if isinstance(loaded.get(key), type(base[key])):
            base[key] = loaded[key]
    return base


def _state() -> dict[str, Any]:
    """The loaded state for the CURRENT data directory (tests switch it)."""
    global _data, _where
    here = str(store.data_dir())
    if _data is None or _where != here:
        _data, _where = _read_state(), here
    return _data


def _touch(force: bool = False) -> None:
    global _dirty, _last_flush
    _dirty = True
    if force or now() - _last_flush >= _FLUSH_EVERY_S:
        flush()


def flush() -> None:
    global _dirty, _last_flush
    with _lock:
        if _data is None or not _dirty:
            return
        try:
            store.write_json(FILE, _data)
        except OSError:
            return  # tried again on the next change
        _dirty = False
        _last_flush = now()


atexit.register(flush)


def reset() -> None:
    """Test helper: forget what is held in memory (the file is left alone)."""
    global _data, _where, _dirty
    global _catalog_version
    with _lock:
        _data, _where, _dirty = None, None, False
        _catalog_version += 1


def snapshot() -> dict[str, Any]:
    import copy

    with _lock:
        return copy.deepcopy(_state())


def _iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else now(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- discovery -------------------------------------------------------------------------------

def _discovered_to_json(d: Discovered) -> dict[str, Any]:
    return {"model_id": d.model_id, "label": d.label, "capabilities": dict(d.capabilities),
            "pricing": d.pricing.as_dict() if isinstance(d.pricing, Pricing) else None,
            "family": d.family, "upstream": d.upstream}


def _discovered_from_json(row: dict[str, Any]) -> Discovered:
    price = row.get("pricing")
    return Discovered(model_id=row["model_id"], label=row.get("label"),
                      capabilities=row.get("capabilities") or {},
                      pricing=Pricing(price["input"], price["output"], price.get("cached_input")) if price else None,
                      family=row.get("family"), upstream=row.get("upstream"))


def record_discovery(connection: str, models: list[Discovered] | None, error: str | None = None) -> None:
    """A successful listing replaces the last; a failed one keeps it and says why."""
    with _lock:
        entry = _state()["discovery"].setdefault(connection, {})
        entry["at"] = _iso()
        entry["ok"] = models is not None
        entry["error"] = error
        if models is not None:
            entry["models"] = [_discovered_to_json(m) for m in models]
            entry["ok_at"] = entry["at"]
            for eid, h in _state()["health"].items():
                if eid.startswith(connection + "/"):
                    h.pop("missing", None)
        _catalog_changed()
        _touch(force=True)


def mark_untested(connection: str) -> None:
    """Its address or key changed: what was last checked no longer describes it."""
    with _lock:
        entry = _state()["discovery"].get(connection)
        if entry is not None:
            entry["ok"] = None
            entry["error"] = None
        _state()["connections"].pop(connection, None)
        _touch(force=True)


def discovered(connection: str) -> list[Discovered]:
    with _lock:
        rows = (_state()["discovery"].get(connection) or {}).get("models") or []
        return [_discovered_from_json(r) for r in rows if isinstance(r, dict) and r.get("model_id")]


def discovery_status(connection: str) -> dict[str, Any]:
    with _lock:
        entry = _state()["discovery"].get(connection) or {}
        return {k: entry.get(k) for k in ("at", "ok", "error", "ok_at")}


def forget_discovered_model(connection: str, model_id: str) -> None:
    with _lock:
        entry = _state()["discovery"].get(connection) or {}
        entry["models"] = [r for r in entry.get("models") or [] if r.get("model_id") != model_id]
        _catalog_changed()
        _touch(force=True)


def forget_connection(connection: str) -> None:
    with _lock:
        s = _state()
        s["discovery"].pop(connection, None)
        s["connections"].pop(connection, None)
        prefix = f"{connection}/"
        for key in ("probes", "latency", "health", "observed"):
            for eid in [e for e in s[key] if e.startswith(prefix)]:
                s[key].pop(eid, None)
        _catalog_changed()
        _touch(force=True)


# --- probes ----------------------------------------------------------------------------------

def record_probe(endpoint_id: str, capabilities: dict[str, Any], results: dict[str, Any]) -> None:
    with _lock:
        _state()["probes"][endpoint_id] = {"at": _iso(), "capabilities": capabilities, "results": results}
        _catalog_changed()
        _touch(force=True)


def probed() -> dict[str, dict[str, Any]]:
    """What probes measured, with any context window observed from a real refusal folded in
    (the smaller of the two wins: the model itself said no above it). A config value still
    beats both (catalog.build)."""
    with _lock:
        out = {eid: dict(p.get("capabilities") or {}) for eid, p in _state()["probes"].items()}
        for eid, seen in _state()["observed"].items():
            window = seen.get("max_context_tokens")
            if isinstance(window, int):
                caps = out.setdefault(eid, {})
                known = caps.get("max_context_tokens")
                caps["max_context_tokens"] = min(known, window) if isinstance(known, int) else window
        return out


def record_observed_context(endpoint_id: str, tokens: int) -> int:
    """An upper bound on a model's context window, observed when it refused a request as too
    long. Dated: it expires (`expire_observed`), and Discover or Test clear it. Kept apart from
    probe results so a later probe run cannot silently drop it, and only ever lowered while it
    lives. Returns the window now on record."""
    tokens = max(1, int(tokens))
    with _lock:
        observed = _state()["observed"]
        previous = (observed.get(endpoint_id) or {}).get("max_context_tokens")
        window = min(previous, tokens) if isinstance(previous, int) else tokens
        observed[endpoint_id] = {"ts": now(), "at": _iso(), "max_context_tokens": window}
        _catalog_changed()
        _touch(force=True)
        return window


def observed_context(endpoint_id: str) -> dict[str, Any] | None:
    with _lock:
        seen = _state()["observed"].get(endpoint_id)
        return dict(seen) if isinstance(seen, dict) else None


def expire_observed(ttl_days: float) -> None:
    """Forget windows observed longer ago than `ttl_days` (a model's limit can be raised)."""
    with _lock:
        observed = _state()["observed"]
        stale = [e for e, row in observed.items()
                 if not isinstance(row.get("ts"), (int, float)) or now() - row["ts"] > ttl_days * 86400]
        for eid in stale:
            observed.pop(eid, None)
        if stale:
            _catalog_changed()
            _touch(force=True)


def clear_observed(connection: str | None = None, endpoint_id: str | None = None) -> None:
    """The person re-checked (Discover, Test): what was observed no longer stands."""
    with _lock:
        observed = _state()["observed"]
        gone = [e for e in observed if e == endpoint_id or (connection and e.startswith(connection + "/"))]
        for eid in gone:
            observed.pop(eid, None)
        if gone:
            _catalog_changed()
            _touch(force=True)


# --- latency ---------------------------------------------------------------------------------

def record_latency(endpoint_id: str, ttft_ms: int | None, total_ms: int | None) -> None:
    """A moving average, so one slow answer nudges rather than defines it."""
    with _lock:
        entry = _state()["latency"].setdefault(endpoint_id, {"samples": 0})
        for key, value in (("ttft_ms", ttft_ms), ("total_ms", total_ms)):
            if value is None:
                continue
            old = entry.get(key)
            entry[key] = value if old is None else int(round(0.6 * old + 0.4 * value))
        entry["samples"] = int(entry.get("samples") or 0) + 1
        _touch()


def latency_ms(endpoint_id: str) -> int | None:
    with _lock:
        entry = _state()["latency"].get(endpoint_id) or {}
        return entry.get("ttft_ms")


# --- health: the per-endpoint breaker, per-connection rests ------------------------------------

def record_success(endpoint_id: str) -> None:
    with _lock:
        entry = _state()["health"].setdefault(endpoint_id, {})
        entry.update({"streak": 0, "open_until": None, "last_ok": _iso()})
        _touch()


def record_failure(endpoint_id: str, message: str, *, threshold: int, base_s: float, max_s: float) -> None:
    """After `threshold` failures in a row the endpoint rests: base_s, doubling with
    every further failure, up to max_s. Any answer ends the rest."""
    with _lock:
        entry = _state()["health"].setdefault(endpoint_id, {})
        streak = int(entry.get("streak") or 0) + 1
        entry["streak"] = streak
        entry["last_error"] = message[:300]
        entry["last_fail"] = _iso()
        if streak >= threshold:
            entry["open_until"] = now() + min(base_s * 2 ** (streak - threshold), max_s)
        _touch()


def restore_streak(endpoint_id: str, streak: int, last_error: str) -> None:
    """Carry failures-in-a-row over from before (no rest is started by it)."""
    with _lock:
        entry = _state()["health"].setdefault(endpoint_id, {})
        entry.update({"streak": streak, "last_error": last_error[:300]})
        _touch()


def resting_until(endpoint_id: str) -> float | None:
    """When the endpoint's circuit breaker lets it be tried again, if it is open."""
    with _lock:
        until = (_state()["health"].get(endpoint_id) or {}).get("open_until")
        return until if isinstance(until, (int, float)) and until > now() else None


def endpoint_rest(endpoint_id: str) -> dict[str, Any] | None:
    """A rest that is about this one endpoint alone: a rate limit (`rate_until`), a daily
    allowance used up (`reset`), or the server saying it has no such model (`missing`, until
    the model list is next fetched)."""
    with _lock:
        entry = _state()["health"].get(endpoint_id) or {}
        if entry.get("missing"):
            return {"kind": "missing", "reason": entry.get("missing_reason") or "it isn't there"}
        until = entry.get("rate_until")
        if isinstance(until, (int, float)) and until > now():
            return {"kind": entry.get("rate_kind") or "rate", "until": until,
                    "reason": entry.get("rate_reason") or "it asked to slow down"}
        return None


def rest_endpoint(endpoint_id: str, seconds: float, *, reason: str, kind: str = "rate") -> None:
    """The provider limited this endpoint for `seconds` (kind "reset": until its own reset)."""
    with _lock:
        entry = _state()["health"].setdefault(endpoint_id, {})
        entry.update({"rate_until": max(float(entry.get("rate_until") or 0), now() + seconds),
                      "rate_kind": kind, "rate_reason": reason[:300]})
        _touch(force=True)


def mark_model_missing(endpoint_id: str, reason: str) -> None:
    """The server said it has no such model: this endpoint is out until Discover lists it again."""
    with _lock:
        entry = _state()["health"].setdefault(endpoint_id, {})
        entry.update({"missing": True, "missing_reason": reason[:300]})
        _touch(force=True)


def siblings_rate_limited(connection: str, endpoint_id: str) -> bool:
    """Is another model of this connection resting from a rate limit right now?"""
    with _lock:
        for eid, entry in _state()["health"].items():
            if eid != endpoint_id and eid.startswith(connection + "/") and entry.get("rate_kind") == "rate":
                until = entry.get("rate_until")
                if isinstance(until, (int, float)) and until > now():
                    return True
        return False


def health(endpoint_id: str) -> dict[str, Any]:
    with _lock:
        return dict(_state()["health"].get(endpoint_id) or {})


def mark_connection_down(connection: str, reason: str, rest_s: float) -> None:
    with _lock:
        entry = _state()["connections"].setdefault(connection, {})
        entry.update({"down_until": now() + rest_s, "down_reason": reason[:300]})
        _touch(force=True)


def mark_connection_up(connection: str) -> None:
    with _lock:
        entry = _state()["connections"].setdefault(connection, {})
        entry.update({"down_until": None, "down_reason": None})
        _touch(force=True)


def connection_down(connection: str) -> str | None:
    with _lock:
        entry = _state()["connections"].get(connection) or {}
        until = entry.get("down_until")
        if isinstance(until, (int, float)) and until > now():
            return entry.get("down_reason") or "it didn't answer"
        return None


def refuse_connection(connection: str, reason: str, rest_s: float, kind: str = "auth") -> None:
    """The connection refused the key or the account (billing, quota): every model on it
    would say the same, so the whole connection rests. Kept in the state file, so a
    restart doesn't clear it; editing the key, reconnecting or the cooldown does."""
    with _lock:
        entry = _state()["connections"].setdefault(connection, {})
        entry.update({"refused_until": now() + rest_s, "refused_reason": reason[:300], "refused_kind": kind})
        _touch(force=True)


def connection_refused(connection: str) -> str | None:
    with _lock:
        entry = _state()["connections"].get(connection) or {}
        until = entry.get("refused_until")
        if isinstance(until, (int, float)) and until > now():
            return entry.get("refused_reason") or "it refused the key"
        return None


def connection_refused_kind(connection: str) -> str:
    """"auth" (the key) or "billing" (the account's credit or quota)."""
    with _lock:
        return str((_state()["connections"].get(connection) or {}).get("refused_kind") or "auth")


def clear_rests(connection: str) -> None:
    """The person acted on it (edited the key, Test, Discover, added it again): every rest
    that was about this connection ends - its own, and each of its endpoints'."""
    with _lock:
        changed = False
        entry = _state()["connections"].get(connection)
        if entry:
            for key in ("refused_until", "refused_reason", "refused_kind", "rate_limited_until"):
                changed = entry.pop(key, None) is not None or changed
        for eid, h in _state()["health"].items():
            if eid.startswith(connection + "/"):
                for key in ("streak", "open_until", "rate_until", "rate_kind", "rate_reason", "missing",
                            "missing_reason"):
                    changed = h.pop(key, None) is not None or changed
        if changed:
            _touch(force=True)


def rate_limit(connection: str, seconds: float) -> None:
    with _lock:
        entry = _state()["connections"].setdefault(connection, {})
        entry["rate_limited_until"] = max(float(entry.get("rate_limited_until") or 0), now() + seconds)
        _touch(force=True)


def rate_limited_until(connection: str) -> float | None:
    with _lock:
        until = (_state()["connections"].get(connection) or {}).get("rate_limited_until")
        return until if isinstance(until, (int, float)) and until > now() else None


# --- spend -----------------------------------------------------------------------------------

def _month() -> str:
    return datetime.fromtimestamp(now(), timezone.utc).strftime("%Y-%m")


def add_spend(usd: float) -> None:
    with _lock:
        spend = _state()["spend"]
        if spend.get("month") != _month():
            spend.update({"month": _month(), "usd": 0.0})
        spend["usd"] = float(spend.get("usd") or 0.0) + usd
        _touch()


def month_spend() -> float:
    with _lock:
        spend = _state()["spend"]
        return float(spend.get("usd") or 0.0) if spend.get("month") == _month() else 0.0


# --- notices for the person ------------------------------------------------------------------

def add_notice(text: str) -> None:
    with _lock:
        _state()["notices"].append({"at": _iso(), "text": text})
        _touch(force=True)


def notices() -> list[dict[str, Any]]:
    with _lock:
        return list(_state()["notices"])
