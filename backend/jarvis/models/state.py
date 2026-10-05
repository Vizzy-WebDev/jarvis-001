"""The state file — what the layer has learned, kept apart from what the person
configured: the last discovery per connection, probe results, latency, endpoint
health (the circuit breaker), connection rests (rate limits, unreachable, a refused
key or account), and this month's spend.

Never written to config. Held in memory and written to `data/models_state.json`
at most once a second (and at exit); losing the last second of latency samples
to a crash costs nothing.
"""

from __future__ import annotations

import atexit
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from .. import store
from .catalog import Pricing
from .prepared import Discovered

FILE = "models_state"
VERSION = 1
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
            "connections": {}, "spend": {"month": None, "usd": 0.0}, "notices": [], "learned": {}}


def _state() -> dict[str, Any]:
    """The loaded state for the CURRENT data directory (tests switch it)."""
    global _data, _where
    here = str(store.data_dir())
    if _data is None or _where != here:
        loaded = store.read_json(FILE, None)
        base = _empty()
        if isinstance(loaded, dict):
            for key in base:
                if isinstance(loaded.get(key), type(base[key])):
                    base[key] = loaded[key]
        _data, _where = base, here
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
        for key in ("probes", "latency", "health", "learned"):
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
    """What probes measured, with any context window learned from a real refusal folded in
    (the smaller of the two wins: the model itself said no above it)."""
    with _lock:
        out = {eid: dict(p.get("capabilities") or {}) for eid, p in _state()["probes"].items()}
        for eid, learned in _state()["learned"].items():
            window = learned.get("max_context_tokens")
            if isinstance(window, int):
                caps = out.setdefault(eid, {})
                known = caps.get("max_context_tokens")
                caps["max_context_tokens"] = min(known, window) if isinstance(known, int) else window
        return out


def record_learned_context(endpoint_id: str, tokens: int) -> int:
    """An upper bound on a model's context window, learned when it refused a request as too
    long. Kept apart from probe results so a later probe run cannot silently drop it, and only
    ever lowered. Returns the window now on record."""
    tokens = max(1, int(tokens))
    with _lock:
        learned = _state()["learned"]
        previous = (learned.get(endpoint_id) or {}).get("max_context_tokens")
        window = min(previous, tokens) if isinstance(previous, int) else tokens
        learned[endpoint_id] = {"at": _iso(), "max_context_tokens": window}
        _catalog_changed()
        _touch(force=True)
        return window


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
    with _lock:
        until = (_state()["health"].get(endpoint_id) or {}).get("open_until")
        return until if isinstance(until, (int, float)) and until > now() else None


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


def refuse_connection(connection: str, reason: str, rest_s: float) -> None:
    """The connection refused the key or the account (billing, quota): every model on it
    would say the same, so the whole connection rests. Kept in the state file, so a
    restart doesn't clear it; editing the key, reconnecting or the cooldown does."""
    with _lock:
        entry = _state()["connections"].setdefault(connection, {})
        entry.update({"refused_until": now() + rest_s, "refused_reason": reason[:300]})
        _touch(force=True)


def connection_refused(connection: str) -> str | None:
    with _lock:
        entry = _state()["connections"].get(connection) or {}
        until = entry.get("refused_until")
        if isinstance(until, (int, float)) and until > now():
            return entry.get("refused_reason") or "it refused the key"
        return None


def clear_refusal(connection: str) -> None:
    """The person reconnected it (Test, Discover, or added it again)."""
    with _lock:
        entry = _state()["connections"].get(connection)
        if entry and ("refused_until" in entry or "refused_reason" in entry):
            entry.pop("refused_until", None)
            entry.pop("refused_reason", None)
            _touch(force=True)


def rate_limit(connection: str, seconds: float) -> None:
    with _lock:
        entry = _state()["connections"].setdefault(connection, {})
        entry["rate_limited_until"] = max(float(entry.get("rate_limited_until") or 0), now() + seconds)
        _touch()


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
