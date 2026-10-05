"""What a unit of usage actually costs — three sources, in write-time precedence.

1. **A price the user set** always wins and is never silently replaced.
2. **A provider's own published numeric pricing** comes next: the prices a
   connected gateway's own model list reported (the model layer's discovery).
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

**A billing tier this build GUESSED is never a price.** The old build inferred
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

#: The same interlock every background timer in this build uses: a refresh that
#: reaches the network must be switched on deliberately, never merely by
#: importing a module.
ENABLE_ENV = "JARVIS_COST_REFRESH"


def is_refresh_enabled() -> bool:
    return os.environ.get(ENABLE_ENV) == "1"


def set_user_price(*, provider: str, model_id: str, unit_kind: str = "tokens",
                   price_in: float | None, price_out: float | None,
                   currency: str = "USD") -> None:
    store.set_price(provider=provider, model_id=model_id, unit_kind=unit_kind,
                    price_in=price_in, price_out=price_out, currency=currency,
                    source="user")


def refresh_reported_prices(*, source: Any = None) -> dict[str, Any]:
    """File the prices connected providers published about their own models.

    `source` is the model layer's `reported_prices()` — what each connection's model
    list said at its last discovery, already per single token and named the way the
    cost ledger names the provider — so `calculate()` multiplies against a raw token
    count with no unit conversion to get wrong. A failure is reported, never raised:
    this is a background refresh, and a caller should not have to guard against it.
    """
    if source is None:
        def source() -> Any:
            from ..models import settings as model_settings
            return model_settings.reported_prices()

    try:
        rows = source()
    except Exception as err:  # noqa: BLE001 — any failure reads the same here
        return {"ok": False, "updated": 0, "error": str(err)}

    updated = 0
    for row in rows or []:
        provider, model_id = row.get("provider"), row.get("model_id")
        price_in, price_out = _as_price(row.get("price_in")), _as_price(row.get("price_out"))
        if not provider or not model_id or (price_in is None and price_out is None):
            continue
        existing = store.get_price(provider, model_id, "tokens")
        if existing and existing.get("source") == "user":
            continue                       # an explicit user price is never replaced
        store.set_price(provider=provider, model_id=model_id, unit_kind="tokens",
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

    Behind the cost timers' interlock, like every background clock. Seeding is
    NOT: it needs no network, writes a fact rather than a poll, is idempotent and never
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
        result = refresh_reported_prices()
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
