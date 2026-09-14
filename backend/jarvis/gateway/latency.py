"""How quickly a deployment starts answering, measured rather than authored.

The old model row carried `tier.speed` — a 1-5 number chosen by matching the
model's NAME against a few regular expressions, which is how an unbounded
`mini` matched inside "ge**mini**" and scored every Gemini model, Pro included,
as a fast one. The catalog deleted that field on the grounds that speed is
measurable and a measured number beats an authored one. This is the measurement
that makes deleting it honest rather than merely tidy: without it the router's
`balance == "fast"` branch would have nothing to rank on and would quietly
collapse into ranking by price.

**Time to FIRST TOKEN, not total duration.** Total duration is mostly a
function of how long the answer was, so a model asked a harder question would
look slower than the same model asked an easier one, and the ranking would
learn the shape of recent questions rather than anything about the model. Time
to first token is the part the person actually waits through, and it does not
move with answer length.

**Keyed per deployment, exactly as cooldowns are.** The same model reached
through a reselling gateway and through the maker's own API genuinely differs
in latency — that extra hop is the whole reason the deployment axis exists —
and averaging them together would produce a number describing neither.

**A tier, not a millisecond count, is what the router reads.** The score is a
weighted sum whose spread the availability bonus was deliberately sized
against; feeding raw milliseconds into it would swamp every other term. The
buckets below land in the 1-5 domain the authored `speed` already used, so
every weight in `routing._score` stays exactly as tuned.
"""

from __future__ import annotations

import threading
from typing import Any

from ..store import read_json, write_json

FILE = "model-latency"

#: Upper bound in milliseconds -> the speed tier for anything at or below it.
#: Higher is faster, matching the field this replaces. Ordered fastest first.
BUCKETS: tuple[tuple[float, int], ...] = (
    (400.0, 5), (900.0, 4), (2000.0, 3), (5000.0, 2),
)
SLOWEST = 1

#: How much a new sample moves the average. High enough that a model which has
#: genuinely got slower is reflected within a few turns, low enough that one
#: unlucky round trip does not re-rank the roster.
ALPHA = 0.3

#: One sample is not an average. A first call to a cold endpoint is routinely
#: several times slower than every call after it, and answering with a tier
#: after one of those would bench a fast model on its own warm-up. Below this,
#: the reading is kept but no opinion is offered and the router uses its
#: neutral default.
MIN_SAMPLES = 2

_lock = threading.RLock()
_cache: dict[str, dict[str, Any]] | None = None


def _load() -> dict[str, dict[str, Any]]:
    global _cache
    with _lock:
        if _cache is None:
            stored = read_json(FILE, {}) or {}
            _cache = stored.get("deployments", {}) if isinstance(stored, dict) else {}
        return _cache


def _flush() -> None:
    """Write through. Called with the lock held."""
    write_json(FILE, {"deployments": _cache or {}})


def record(deployment_id: str, first_token_ms: float) -> None:
    """Note how long this deployment took to say its first word."""
    if not deployment_id or first_token_ms is None or first_token_ms < 0:
        return
    with _lock:
        rows = _load()
        row = rows.get(deployment_id) or {}
        previous = row.get("firstTokenMs")
        blended = (float(first_token_ms) if not isinstance(previous, (int, float))
                   else previous + ALPHA * (float(first_token_ms) - previous))
        rows[deployment_id] = {
            "firstTokenMs": round(blended, 1),
            "samples": int(row.get("samples") or 0) + 1,
        }
        _flush()


def first_token_ms(deployment_id: str) -> float | None:
    """The smoothed average, or None if nothing has been measured."""
    row = _load().get(deployment_id) or {}
    value = row.get("firstTokenMs")
    return float(value) if isinstance(value, (int, float)) else None


def speed_tier(deployment_id: str) -> int | None:
    """1-5, higher is faster. None when there is not yet enough to say.

    None is a real answer and the router treats it as one: it falls back to a
    neutral value rather than inventing a reading, for the same reason
    `cost/advisor.py` returns None rather than a made-up price. A guess wearing
    the authority of a measurement is worse than no measurement.
    """
    row = _load().get(deployment_id) or {}
    if int(row.get("samples") or 0) < MIN_SAMPLES:
        return None
    average = row.get("firstTokenMs")
    if not isinstance(average, (int, float)):
        return None
    for ceiling, tier in BUCKETS:
        if average <= ceiling:
            return tier
    return SLOWEST


def clear(deployment_id: str) -> None:
    """Forget a deployment's readings — it was removed, or its id reused."""
    with _lock:
        rows = _load()
        if rows.pop(deployment_id, None) is not None:
            _flush()


def reset_for_tests() -> None:
    global _cache
    with _lock:
        _cache = None
