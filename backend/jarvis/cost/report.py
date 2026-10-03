"""The read side: what has actually been spent, built only from real rows.

Three labelled sections that are never merged — see this package's docstring.
`pricelessGroups` is the honest part: a group with no price on record is listed
by name so the answer can say "this much of it has no known price" rather than
quietly under-reporting the total.

`freeGroups` is the other half of that honesty. A group that cost $0 because the
model is genuinely free is reported as free, by name — not left to be inferred
from an amount of 0.00, which a paid model also produces on a quiet enough day.
Free, unpriced and "used but cost nothing this period" are three different facts.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..jscompat import to_iso_z
from . import store
from .prices import calculate


def _start_of_day(now: datetime) -> str:
    return to_iso_z(datetime(now.year, now.month, now.day, tzinfo=timezone.utc))


def _start_of_month(now: datetime) -> str:
    return to_iso_z(datetime(now.year, now.month, 1, tzinfo=timezone.utc))


def usage_breakdown(since_iso: str) -> dict[str, Any]:
    events = store.list_events_since(since_iso)
    groups: dict[tuple[str, str | None, str], dict[str, Any]] = {}

    for event in events:
        key = (event["provider"], event["modelId"], event["unitKind"])
        group = groups.setdefault(key, {
            "provider": event["provider"], "modelId": event["modelId"],
            "unitKind": event["unitKind"], "unitsIn": 0, "unitsOut": 0,
            "cachedIn": 0, "calls": 0,
        })
        group["unitsIn"] += event["unitsIn"] or 0
        group["unitsOut"] += event["unitsOut"] or 0
        group["cachedIn"] += event["cachedIn"] or 0
        group["calls"] += 1

    rows: list[dict[str, Any]] = []
    priceless: list[dict[str, Any]] = []
    free: list[dict[str, Any]] = []
    total = 0.0
    currency: str | None = None
    priced = False

    for group in groups.values():
        money = calculate(provider=group["provider"], model_id=group["modelId"],
                          unit_kind=group["unitKind"], units_in=group["unitsIn"],
                          units_out=group["unitsOut"])
        named = {"provider": group["provider"], "modelId": group["modelId"],
                 "unitKind": group["unitKind"]}
        if money is None:
            priceless.append(named)
            rows.append({**group, "calculated": None})
            continue
        priced = True
        total += money["amount"]
        currency = currency or money["currency"]
        if money.get("free"):
            free.append(named)
        rows.append({**group, "calculated": money})

    return {
        "since": since_iso,
        "eventCount": len(events),
        "measured": {"groups": rows},
        # `priced` rather than "any row has a non-zero amount": a real $0 total
        # made entirely of free models is a known total, not a missing one.
        "calculated": ({"amount": total, "currency": currency or "USD"}
                       if priced else None),
        "pricelessGroups": priceless,
        "freeGroups": free,
        "providerReported": store.list_balances(),
    }


def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now(timezone.utc)


def today(now: datetime | None = None) -> dict[str, Any]:
    return usage_breakdown(_start_of_day(_now(now)))


def month_to_date(now: datetime | None = None) -> dict[str, Any]:
    return usage_breakdown(_start_of_month(_now(now)))


def last_days(days: int, now: datetime | None = None) -> dict[str, Any]:
    since = _now(now) - timedelta(days=max(1, days))
    return usage_breakdown(to_iso_z(since))


def most_used(since_iso: str) -> dict[str, Any] | None:
    """Which model was used most — a real count of recorded calls, never inferred
    from anything else."""
    groups = usage_breakdown(since_iso)["measured"]["groups"]
    return max(groups, key=lambda g: g["calls"], default=None)
