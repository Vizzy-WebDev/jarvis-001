"""Data policy and the budget — the only two things that may exclude an endpoint for
reasons other than what it can do.

`data_class` is descriptive. It never excludes an endpoint by itself: the shipped
policy is empty, which allows every data class on every trust class. Only a data
class the person has listed under `policies.data_classes` is restricted, and then
the restriction holds on every call — pinned or not, first attempt or fallback.
"""

from __future__ import annotations

from .catalog import Endpoint
from .config import Config


def allowed_trust(cfg: Config, data_class: str) -> frozenset[str] | None:
    """The trust classes this data may go to, or None for no restriction."""
    return cfg.policy.data_classes.get(data_class)


def trust_allows(cfg: Config, data_class: str, trust: str) -> bool:
    allowed = allowed_trust(cfg, data_class)
    return allowed is None or trust in allowed


def budget_spent(cfg: Config, month_spend: float) -> bool:
    budget = cfg.policy.monthly_budget_usd
    return budget is not None and month_spend >= budget


def usable_after_budget(endpoint: Endpoint) -> bool:
    """Once the month's budget is spent, only a known price of zero stays usable.
    An unknown price is not free."""
    return endpoint.pricing is not None and endpoint.pricing.free
