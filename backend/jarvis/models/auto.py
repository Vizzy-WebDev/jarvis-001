"""How Auto chooses — and the promise that it never touches an explicit choice.

Auto is one of the person's two ways to pick a model. The other is naming one, and
that is honoured exactly: this module is not consulted for it at all.

When Auto is chosen, Jarvis picks per turn from the models that are set up now,
by rules that are written down here and never involve chance:

1. **Who can be picked.** Every model on a connection that can be used (it has its
   key, where one is needed), except one the PROVIDER has itself said cannot take
   a Jarvis turn — a video model, one that doesn't produce text, one that says tool
   calling doesn't work — and, when the turn carries a picture, one that said it
   doesn't accept images. Nothing is excluded on a guess from its name: a provider
   that reports nothing about a model leaves it in.
2. **Who is preferred.** Models that have answered here before come first — from
   the outcomes recorded, or from the replies already saved in the conversation —
   the one that answered most recently at the top, so Auto is steady, not restless.
   Untried models follow, those the provider reported as tool-capable ahead of
   those it said nothing about, then in the order they were connected and listed.
3. **Who is steered around.** A model that failed a moment ago waits behind the
   others: a few minutes for a hiccup (busy, rate-limited), longer for a real
   refusal (wrong key, not allowed, no such model, a request it can't use). When a
   provider says the account needs credit (402), the models it lists as paid wait
   too, and the ones it lists as free do not. Moved down, never removed - if
   nothing else is left it is still tried.

What Auto does with that list is in `client.py`: try the first, and if it fails
before saying anything, the next, a few times at most, telling the person which
model answered and why the earlier ones didn't.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .. import config
from . import kinds, store

#: How long a failed model waits behind the others, by what the failure was.
COOLDOWN_HICCUP = timedelta(minutes=5)   # busy, rate-limited, dropped, garbled
COOLDOWN_REFUSED = timedelta(minutes=30)  # key, permission, no such model, unusable request
_HICCUPS = {"rate", "server", "network", "reply"}

#: Auto tries at most this many models for one step, and starts no new attempt once it
#: has spent this long — failures are usually quick, and a hang must not multiply.
MAX_ATTEMPTS = 6
TIME_BUDGET_S = 75.0
#: While another model is still waiting its turn, a silent one is given this long.
FAST_READ_TIMEOUT_S = 45.0


@dataclass(frozen=True)
class Candidate:
    connection: store.Connection
    model: store.Model


def lacks_key(connection: store.Connection) -> bool:
    """A connection whose kind needs a key and has none saved."""
    kind = kinds.KINDS.get(connection.kind)
    return bool(kind and kind.key == "required"
                and not (connection.secret_ref and config.get_secret(connection.secret_ref)))


def fits(model: store.Model, *, needs_images: bool = False) -> bool:
    """Can this model take a turn, going only by what its provider said about it?"""
    facts = model.facts or {}
    if facts.get("chat") is False or facts.get("tools") is False:
        return False
    if needs_images and facts.get("image") is False:
        return False
    return True


def _moment(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def waiting(outcome: store.Outcome | None, now: datetime) -> bool:
    """Did this model fail recently enough that the others should go first?"""
    if not outcome or not outcome.last_fail_at:
        return False
    failed = _moment(outcome.last_fail_at)
    if failed is None:
        return False
    worked = _moment(outcome.last_ok_at)
    if worked is not None and worked >= failed:
        return False  # it answered after that failure — the failure is history
    length = COOLDOWN_HICCUP if outcome.fail_kind in _HICCUPS else COOLDOWN_REFUSED
    return now - failed < length


def candidates(*, needs_images: bool = False, now: datetime | None = None) -> list[Candidate]:
    """Everything Auto may pick, best first. Deterministic: the same models, the same
    outcomes and the same clock always give the same order."""
    now = now or datetime.now(timezone.utc)
    outcomes = store.list_outcomes()
    # "This account needs credit" (402) is about the account, not one model - but only for
    # models that cost something. A connection where one paid model just asked for credit
    # holds back the others its provider lists as paid; models it lists as free carry on.
    out_of_credit: dict[str, datetime] = {}
    for (connection_id, _), o in outcomes.items():
        failed = _moment(o.last_fail_at)
        if o.fail_kind == "billing" and failed and now - failed < COOLDOWN_REFUSED and (
                connection_id not in out_of_credit or failed > out_of_credit[connection_id]):
            out_of_credit[connection_id] = failed
    answered = store.recent_answers()
    listed = store.list_models()
    copies: dict[str, int] = {}
    for model in listed:
        copies[model.model_id] = copies.get(model.model_id, 0) + 1
    ranked: list[tuple[tuple, Candidate]] = []
    for connection in store.list_connections():
        if lacks_key(connection):
            continue
        for model in store.list_models(connection.id):
            if not fits(model, needs_images=needs_images):
                continue
            outcome = outcomes.get((connection.id, model.model_id))
            worked = _moment(outcome.last_ok_at) if outcome else None
            # A model that answered in the saved conversation has worked here even if no
            # outcome was recorded then — counted only when its id is on ONE connection,
            # so the credit can't land on the wrong one.
            if worked is None and copies.get(model.model_id) == 1:
                worked = _moment(answered.get(model.model_id))
            held = (connection.id in out_of_credit and (model.facts or {}).get("free") is False
                    and not (worked and worked > out_of_credit[connection.id]))
            key = (
                1 if (held or waiting(outcome, now)) else 0,             # steered around last
                0 if worked else 1,                                      # proven before untried
                -(worked.timestamp()) if worked else 0.0,                # most recent success first
                0 if (model.facts or {}).get("tools") is True else 1,   # reported tool-capable first
                connection.created_at, model.added_at, model.model_id,  # then the order it was set up
            )
            ranked.append((key, Candidate(connection, model)))
    ranked.sort(key=lambda pair: pair[0])
    return [candidate for _, candidate in ranked]
