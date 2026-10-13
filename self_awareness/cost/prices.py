"""What a unit of usage actually costs, read from the model layer and never stored here.

The model layer already knows each endpoint's price: what the person wrote in their model
settings, else what the connection's own list reported, else $0 for a connection that runs on
their own machine. This module only multiplies a measured count by it. It keeps no table of its
own, so there is nothing to refresh and nothing that can disagree with the model settings.

**"Free" and "no price known" are different answers and must never be collapsed.** A $0 price
is real: it counts toward the total and `report.py` reports it as free. No price at all means
nobody knows, and that model is excluded from the total and named.
"""

from __future__ import annotations

from typing import Any


def _price_of(provider: str, model_id: str) -> tuple[Any, str] | None:
    from ..models import config as model_config
    from ..models import engine
    from ..models.catalog import endpoint_id

    cfg = model_config.current()
    catalog = engine.catalog(cfg)
    for conn in cfg.connections.values():
        if conn.ledger_name != provider:
            continue
        endpoint = catalog.endpoints.get(endpoint_id(conn.name, model_id))
        if endpoint is None or endpoint.pricing is None:
            continue
        entry = conn.models.get(model_id)
        source = ("user" if entry is not None and entry.pricing is not None
                  else "provider_reported" if endpoint.listed else "built_in")
        return endpoint.pricing, source
    return None


def calculate(*, provider: str, model_id: str | None, unit_kind: str = "tokens",
              units_in: int = 0, units_out: int = 0) -> dict[str, Any] | None:
    """A real measured count times a known price, or None, never an estimate. Only token
    usage has a price in the model layer; anything else is unpriced."""
    if not provider or not model_id or unit_kind != "tokens":
        return None
    found = _price_of(provider, model_id)
    if found is None:
        return None
    pricing, source = found
    amount = pricing.cost(input_tokens=units_in or 0, output_tokens=units_out or 0)
    if amount is None:
        return None
    return {"amount": amount, "currency": "USD", "priceSource": source, "free": pricing.free}
