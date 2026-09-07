"""What a unit of usage actually costs — three sources, in write-time precedence.

1. **A price the user set** always wins and is never silently replaced.
2. **A provider's own published numeric pricing** (OpenRouter publishes real
   per-token prices at a public endpoint) comes next.
3. **A built-in** is seeded for exactly one case: a local model costs nothing to
   run. That is the only price this build can assert without an external source.

**No dollar figure is hardcoded for a cloud model.** The catalog names models
faster than any publicly verifiable pricing this build could stand behind, and a
plausible-looking invented number is precisely the failure this whole subsystem
exists to prevent. A cloud model with no user price and no provider-reported
price simply has none, and `report.py` says so instead of costing it at zero.
"""

from __future__ import annotations

import logging
import os
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


def seed_local_model_prices() -> int:
    """A $0 price for every known local model. Never overwrites an existing row:
    a user's own price, or a provider-reported one, must not be downgraded back
    to a built-in — even to the same number."""
    from ..gateway.registry import list_models

    seeded = 0
    for entry in list_models():
        if entry.get("kind") != "local" and entry.get("billing") != "local":
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


def calculate(*, provider: str, model_id: str | None, unit_kind: str = "tokens",
              units_in: int = 0, units_out: int = 0) -> dict[str, Any] | None:
    """A real measured count times a known price.

    Returns None — never 0, never an estimate — when no price is on record. This
    is the only place in the subsystem a money figure is ever produced.
    """
    price = store.get_price(provider, model_id, unit_kind)
    if price is None:
        return None
    in_cost = price["priceIn"] * (units_in or 0) if isinstance(price["priceIn"], (int, float)) else 0.0
    out_cost = price["priceOut"] * (units_out or 0) if isinstance(price["priceOut"], (int, float)) else 0.0
    return {"amount": in_cost + out_cost, "currency": price["currency"],
            "priceSource": price["source"]}
