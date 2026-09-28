"""What a failure holds back, and for how long.

A provider module decides how far a failure reaches (`ProviderError.scope`); this
module only keeps the resulting hold, keyed exactly that far:

* ``model``      — this model, on this credential: `(credential_id, model_id)`;
* ``credential`` — every model reached with this key/account: `credential_id`;
* ``provider``   — everything on this connection: `connection_id`.

(``unknown`` is held like ``model``; ``request`` holds nothing — the request was
wrong, not the model.) A hold lasts as long as the PROVIDER said to wait, when it
said (Retry-After, retryDelay); otherwise 5 minutes after the first failure in a row,
doubling with each further one, capped at 2 hours. Any answer lifts the holds that
answer disproves, and so does the person fixing the connection (a new key, a test
that passes).

Who reads it:

* **Auto** steers around anything held — moved to the back, never removed.
* **A named model** is refused up front only while a hold is one the provider itself
  stated a wait for, or is about the whole credential or connection (a refused key,
  no credit, a host that can't be reached). Jarvis's own backoff guess after an
  ordinary failure never locks the person out of the model they chose.

A hold about money (``kind == "billing"``) does not hold a model the provider listed
as free — nor, where the provider lists prices, one it listed without a price.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from . import store
from .errors import ProviderError

#: How long a model waits after its first failure in a row with no wait stated by the
#: provider. It doubles with each further consecutive failure, up to the cap.
BASE_COOLDOWN = timedelta(minutes=5)
MAX_COOLDOWN = timedelta(hours=2)

#: The scopes that hold more than one model — and so, for a named model, the ones
#: that are about the connection rather than a guess about the model.
WIDE = ("credential", "provider")


def cooldown_for(streak: int) -> timedelta:
    """5 minutes, doubling with each further failure in a row, capped."""
    return min(BASE_COOLDOWN * (2 ** min(max(1, streak) - 1, 8)), MAX_COOLDOWN)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _moment(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def key_for(scope: str, connection: store.Connection, model_id: str) -> str:
    if scope == "model":
        return f"{connection.credential_id}\x1f{model_id}"
    if scope == "credential":
        return connection.credential_id
    return connection.id


def held_scope(err: ProviderError) -> str | None:
    """Where a failure is held: its own scope, `unknown` as `model`, `request` nowhere."""
    if err.scope == "request":
        return None
    return "model" if err.scope == "unknown" else err.scope


def record(connection: store.Connection, model_id: str, err: ProviderError,
           now: datetime | None = None) -> store.Hold | None:
    """Hold back what this failure reaches. Returns the hold, or None for a request error."""
    scope = held_scope(err)
    if scope is None:
        return None
    now = now or datetime.now(timezone.utc)
    key = key_for(scope, connection, model_id)
    previous = store.get_hold(scope, key)
    streak = (previous.streak + 1) if previous else 1
    wait = (timedelta(seconds=err.retry_after_s) if err.retry_after_s is not None
            else cooldown_for(streak))
    hold = store.Hold(scope=scope, hold_key=key, connection_id=connection.id,
                      credential_id=connection.credential_id, model_id=model_id if scope == "model" else None,
                      kind=err.kind, status=err.status, message=str(err), failed_at=_iso(now),
                      until=_iso(now + wait), streak=streak, retry_after_s=err.retry_after_s)
    store.put_hold(hold)
    return hold


def clear_on_success(connection: store.Connection, model_id: str) -> None:
    """An answer disproves this model's hold, its credential's and its connection's."""
    store.delete_holds([(scope, key_for(scope, connection, model_id)) for scope in ("model", *WIDE)])


def clear_connection(connection_id: str) -> None:
    """The person changed the connection (a new key) or proved it works (a test that
    passed): whatever was held against the connection or its credential is lifted."""
    store.delete_connection_holds(connection_id, WIDE)


def billing_exempt(model: store.Model, priced: bool) -> bool:
    """A model a "needs credit" failure does not hold: one the provider listed as free,
    or — on a connection whose provider lists prices at all — one it gave no price for."""
    free = (model.facts or {}).get("free")
    return free is True or (priced and free is None)


def priced_connections(models: Iterable[store.Model]) -> set[str]:
    return {m.provider_id for m in models if isinstance((m.facts or {}).get("free"), bool)}


@dataclass(frozen=True)
class Holds:
    """Every hold still running at `now`, read once — what one planning pass consults."""

    holds: tuple[store.Hold, ...]
    now: datetime
    priced: frozenset[str] = frozenset()

    @classmethod
    def load(cls, now: datetime | None = None, models: Iterable[store.Model] | None = None) -> "Holds":
        now = now or datetime.now(timezone.utc)
        running = tuple(h for h in store.list_holds() if (_moment(h.until) or now) > now)
        listed = list(models) if models is not None else store.list_models()
        return cls(holds=running, now=now, priced=frozenset(priced_connections(listed)))

    def on(self, connection: store.Connection, model: store.Model) -> list[store.Hold]:
        """The running holds that reach this model."""
        keys = {(scope, key_for(scope, connection, model.model_id)) for scope in ("model", *WIDE)}
        found = []
        for hold in self.holds:
            if (hold.scope, hold.hold_key) not in keys:
                continue
            if hold.kind == "billing" and billing_exempt(model, connection.id in self.priced):
                continue
            found.append(hold)
        return found

    def blocking(self, connection: store.Connection, model: store.Model) -> store.Hold | None:
        """The hold that keeps this model waiting longest, if any — what Auto steers by."""
        return max(self.on(connection, model), key=lambda h: h.until, default=None)

    def refusing(self, connection: store.Connection, model: store.Model) -> store.Hold | None:
        """The hold that stops a NAMED model being tried at all: only one the provider
        stated a wait for, or one about the whole credential or connection."""
        firm = [h for h in self.on(connection, model) if h.retry_after_s is not None or h.scope in WIDE]
        return max(firm, key=lambda h: h.until, default=None)

    def seconds_left(self, hold: store.Hold) -> int:
        until = _moment(hold.until) or self.now
        return max(1, int((until - self.now).total_seconds() + 0.999))
