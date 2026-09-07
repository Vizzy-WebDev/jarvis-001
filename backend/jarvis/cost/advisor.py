"""Cost at decision time: letting the router rank on a MEASURED price rather
than a guess.

The router already weighed `tier.cost` in every scoring branch. What it was
weighing was a 1-5 guess made by matching the model's NAME against a few
regexes. This substitutes a value derived from a real recorded price — in the
exact same 0-4 domain the guess already used, deliberately, so the substitution
is mathematically guaranteed to stay inside the score spread the availability
bonus was sized against. A new scale would have needed the whole ranking
re-tuned to stay safe.

Returns None when there is no real price yet, and the router keeps the catalog's
own guess. A fallback number invented here would be a third guess wearing the
authority of a measurement.
"""

from __future__ import annotations

import time
from typing import Any

from . import store

#: USD per 1,000 tokens, blended across input and output. A coarse bucket, not a
#: cost estimate — `report.py` is where a real figure lives.
BUCKETS: tuple[tuple[float, int], ...] = (
    (0.0005, 0), (0.002, 1), (0.01, 2), (0.03, 3),
)

#: Scoring runs per candidate per turn, so an uncached read would mean a SQLite
#: query per model per turn. Prices change on a daily refresh at most.
CACHE_TTL_S = 60.0
_cache: dict[tuple[str, str], tuple[float, int | None]] = {}


def _bucket_for(usd_per_1k: float) -> int:
    for ceiling, tier in BUCKETS:
        if usd_per_1k <= ceiling:
            return tier
    return 4


def observed_cost_tier(provider: str | None, model_id: str | None) -> int | None:
    if not provider or not model_id:
        return None
    key = (provider, model_id)
    cached = _cache.get(key)
    now = time.monotonic()
    if cached and now - cached[0] < CACHE_TTL_S:
        return cached[1]

    price = store.get_price(provider, model_id, "tokens")
    tier: int | None = None
    if price is not None:
        price_in = price["priceIn"] if isinstance(price["priceIn"], (int, float)) else 0.0
        price_out = price["priceOut"] if isinstance(price["priceOut"], (int, float)) else 0.0
        # Both zero is a genuine free or local model, not missing data: bucket 0
        # is the right answer for it, and None would be wrong.
        tier = _bucket_for(((price_in + price_out) / 2) * 1000)
    _cache[key] = (now, tier)
    return tier


def reset_cache() -> None:
    """After a price write, and in tests. Cheap: the cache is tiny."""
    _cache.clear()
