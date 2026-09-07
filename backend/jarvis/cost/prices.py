"""What a unit of usage actually costs — three sources, in write-time precedence.

1. **A price the user set** always wins and is never silently replaced.
2. **A provider's own published numeric pricing** (OpenRouter publishes real
   per-token prices at a public endpoint) comes next.
3. **A built-in $0** is seeded for exactly two cases, both of them facts rather
   than guesses: a local model costs nothing to run, and a model id ending
   `:free` is the PROVIDER'S own label for a free variant.

**"Free" and "no price known" are different answers and must never be
collapsed.** A $0 row is a real price: it counts toward the total, it puts the
model in the cheapest routing bucket, and `report.py` reports it as free. No row
at all means nobody knows, and that model is excluded from the total and named.
A subsystem that reported an unpriced model as $0 would understate spend, and one
that reported a genuinely free model as unpriced would make a $0 month look like
a month with no data.

**No dollar figure is hardcoded for a cloud model.** The catalog names models
faster than any publicly verifiable pricing this build could stand behind, and a
plausible-looking invented number is precisely the failure this whole subsystem
exists to prevent.

**A billing tier this build GUESSED is never a price.** `catalog.py` infers
`billing: "free"` from a name regex ("flash"), which a paid-tier key matches just
as well. That inference is good enough to rank a model and nowhere near good
enough to assert what it costs, so only `:free` — which the provider itself put
in the id — is seeded here.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

from . import store

logger = logging.getLogger(__name__)

OPENROUTER_CATALOG_URL = "https://openrouter.ai/api/v1/models"

#: The same interlock every background timer in this build uses: a refresh that
#: reaches the network must be switched on deliberately, never merely by
#: importing a module.
ENABLE_ENV = "JARVIS_COST_REFRESH"


def is_refresh_enabled() -> bool:
    return os.environ.get(ENABLE_ENV) == "1"


#: A provider's own suffix for a free variant. Its own label, not our reading of
#: its name — see this module's docstring on why that distinction decides what
#: may be seeded.
FREE_MODEL_SUFFIX = ":free"


def is_known_free(entry: dict[str, Any]) -> bool:
    """Whether this model is free as a FACT rather than as an inference.

    True for a local model (nothing is paid to run it here) and for a model whose
    id carries the provider's own `:free` suffix. Deliberately NOT true for
    `billing == "free"` on its own: that comes from a name regex in the catalog,
    and a paid-tier key matches the same names.
    """
    if entry.get("kind") == "local" or entry.get("billing") == "local":
        return True
    return str(entry.get("model") or "").lower().endswith(FREE_MODEL_SUFFIX)


def seed_known_free_prices() -> int:
    """A $0 price for every model known to be free — see `is_known_free`.

    Never overwrites an existing row: a user's own price, or a provider-reported
    one, must not be downgraded back to a built-in, even to the same number.
    Needs no network and writes a fact, so it is safe to run on every start.
    """
    from ..gateway.registry import list_models

    seeded = 0
    for entry in list_models():
        if not is_known_free(entry):
            continue
        provider = entry.get("adapter") or "local"
        model = entry.get("model")
        if not model or store.get_price(provider, model, "tokens"):
            continue
        store.set_price(provider=provider, model_id=model, unit_kind="tokens",
                        price_in=0.0, price_out=0.0, source="built_in")
        seeded += 1
    return seeded


def set_user_price(*, provider: str, model_id: str, unit_kind: str = "tokens",
                   price_in: float | None, price_out: float | None,
                   currency: str = "USD") -> None:
    store.set_price(provider=provider, model_id=model_id, unit_kind=unit_kind,
                    price_in=price_in, price_out=price_out, currency=currency,
                    source="user")


def refresh_from_openrouter(*, fetch: Any = None) -> dict[str, Any]:
    """Pull OpenRouter's own published per-token prices.

    Stored exactly as given — USD per single token, not per million — so
    `calculate()` multiplies against a raw token count with no unit conversion
    to get wrong. A network failure is reported, never raised: this is a
    background refresh, and a caller should not have to guard against it.
    """
    if fetch is None:
        def fetch() -> Any:
            import httpx
            response = httpx.get(OPENROUTER_CATALOG_URL, timeout=20.0)
            response.raise_for_status()
            return response.json()

    try:
        data = fetch()
    except Exception as err:  # noqa: BLE001 — any transport failure reads the same here
        return {"ok": False, "updated": 0, "error": str(err)}

    models = data.get("data") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return {"ok": False, "updated": 0, "error": "OpenRouter's catalog was not in the expected shape."}

    updated = 0
    for model in models:
        if not isinstance(model, dict):
            continue
        pricing = model.get("pricing")
        model_id = model.get("id")
        if not model_id or not isinstance(pricing, dict):
            continue
        price_in = _as_price(pricing.get("prompt"))
        price_out = _as_price(pricing.get("completion"))
        if price_in is None and price_out is None:
            continue
        existing = store.get_price("openrouter", model_id, "tokens")
        if existing and existing.get("source") == "user":
            continue                       # an explicit user price is never replaced
        store.set_price(provider="openrouter", model_id=model_id, unit_kind="tokens",
                        price_in=price_in, price_out=price_out,
                        source="provider_reported")
        updated += 1
    return {"ok": True, "updated": updated}


def _as_price(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def calculate(*, provider: str, model_id: str | None, unit_kind: str = "tokens",
              units_in: int = 0, units_out: int = 0) -> dict[str, Any] | None:
    """A real measured count times a known price.

    Returns None — never 0, never an estimate — when no price is known. This is
    the only place in the subsystem a money figure is ever produced.

    A row whose two sides are BOTH null is not a known price: it is a row with no
    numbers in it, and multiplying it out would report "this was free" from no
    data at all. "A price row exists" and "a price is known" are different tests.
    """
    price = store.get_price(provider, model_id, unit_kind)
    if price is None:
        return None
    price_in, price_out = _number(price["priceIn"]), _number(price["priceOut"])
    if price_in is None and price_out is None:
        return None
    amount = (price_in or 0.0) * (units_in or 0) + (price_out or 0.0) * (units_out or 0)
    return {"amount": amount, "currency": price["currency"],
            "priceSource": price["source"], "free": price_in == 0.0 and price_out == 0.0}


# --- keeping prices current ---------------------------------------------------

#: Published prices change on the order of months, not hours.
REFRESH_INTERVAL_S = 24 * 60 * 60.0

_timer: threading.Timer | None = None


def start_price_maintenance() -> bool:
    """The periodic pull of provider-published prices.

    Behind the interlock because it reaches the network. Seeding is NOT: it
    needs no network, writes a fact rather than a poll, is idempotent and never
    overwrites — see `assembly.start_background_work`, which seeds either way.

    Without this the refresh existed and was tested but nothing ever called it,
    so a provider's own published prices — including the real zeroes it reports
    for its free variants — were never actually pulled.
    """
    global _timer
    if not is_refresh_enabled() or _timer is not None:
        return False

    def run() -> None:
        global _timer
        result = refresh_from_openrouter()
        if not result.get("ok"):
            logger.info("price refresh did not run: %s", result.get("error"))
        _timer = threading.Timer(REFRESH_INTERVAL_S, run)
        _timer.daemon = True
        _timer.start()

    run()
    return True


def stop_price_maintenance() -> None:
    global _timer
    if _timer is not None:
        _timer.cancel()
        _timer = None
