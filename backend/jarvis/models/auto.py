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
   that reports nothing about a model leaves it in. When a connection's models
   include ones the provider itself marked as its own router (`facts["router"]` —
   reported, e.g. OmniRoute's `owned_by: "combo"`, never guessed from an id), only
   those are candidates on that connection: a router already picks and falls back
   across the rest of that connection's models on its own, so Auto does not also
   walk them individually — that would be trying to do the router's job worse, with
   less information than the router itself has.
2. **Who is preferred.** Models that have answered here before come first — from the
   outcomes recorded, or from the replies already saved in the conversation. Among
   them, quicker ones first (in coarse classes, so Auto stays steady rather than
   restless), then the one that answered most recently. Untried models follow, those
   the provider reported as tool-capable ahead of those it said nothing about, then
   in the order they were connected and listed. **Speed is only ever a preference:**
   a slow model is never excluded, never counted as failing for being slow, and is
   used whenever the quicker ones aren't available.
3. **Who is steered around.** A model that just failed waits behind the others, for
   5 minutes after its first failure in a row and twice as long for each further one
   (capped at 2 hours); any answer clears it. One rule for every kind of failure, so a
   model with a good record isn't shelved for long over a single blip. Some failures
   are about the whole connection rather than one model — the key is refused, the
   host can't be reached — and those hold back every model on it; a provider saying
   the account needs credit (402) holds back the models it lists as paid, and not the
   ones it lists as free. Moved down, never removed: if nothing else is left it is
   still tried.

What Auto does with that list is in `attempt.py`: try the first and, only if it
FAILS before saying anything, the next — never because a model is merely slow.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .. import config
from . import kinds, store

#: How long a model waits behind the others after its first failure in a row. It
#: doubles with each further consecutive failure, up to the cap; an answer resets it.
BASE_COOLDOWN = timedelta(minutes=5)
MAX_COOLDOWN = timedelta(hours=2)

#: Failures that say something about the WHOLE connection, not one model on it.
CONNECTION_WIDE = {"auth", "unreachable"}
#: Models that may fail on one connection in one step before it is left alone for that step.
STRIKES_PER_CONNECTION = 2
#: Once this long has passed in a step, after a failure, Auto stops STARTING models it has
#: never seen work. It gates starting new attempts only — it never touches a request that
#: is in progress, however long that has been thinking.
FIND_BUDGET_S = 30.0

#: Speed classes for models that have answered, by how long they take to start.
FAST_MS = 3000
OK_MS = 8000


@dataclass(frozen=True)
class Candidate:
    connection: store.Connection
    model: store.Model
    #: It has answered here before.
    proven: bool = False


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


def cooldown(outcome: store.Outcome) -> timedelta:
    """How long this model waits after its latest failure: 5 minutes, doubling with each
    further failure in a row, capped."""
    streak = max(1, outcome.fail_streak)
    return min(BASE_COOLDOWN * (2 ** min(streak - 1, 8)), MAX_COOLDOWN)


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
    return now - failed < cooldown(outcome)


def speed_class(ttft_ms: int | None) -> int:
    """0 quick, 1 middling (or not measured), 2 slow. Coarse on purpose."""
    if ttft_ms is None:
        return 1
    return 0 if ttft_ms <= FAST_MS else 1 if ttft_ms <= OK_MS else 2


def _holds(outcomes: dict[tuple[str, str], store.Outcome],
           now: datetime) -> tuple[dict[str, datetime], dict[str, datetime]]:
    """`(whole, paid)`: for each connection, when it last failed in a way that holds back
    ALL its models (key refused, host unreachable) or only its PAID ones (needs credit) —
    for as long as that failure is still fresh."""
    whole: dict[str, datetime] = {}
    paid: dict[str, datetime] = {}
    last_ok: dict[str, datetime] = {}
    for (connection_id, _), o in outcomes.items():
        ok = _moment(o.last_ok_at)
        if ok and (connection_id not in last_ok or ok > last_ok[connection_id]):
            last_ok[connection_id] = ok
        failed = _moment(o.last_fail_at)
        if not failed or now - failed >= cooldown(o):
            continue
        held = whole if o.fail_kind in CONNECTION_WIDE else paid if o.fail_kind == "billing" else None
        if held is not None and (connection_id not in held or failed > held[connection_id]):
            held[connection_id] = failed
    # Anything on the connection answering since the failure means the connection is fine again.
    for connection_id in [c for c, failed in whole.items() if last_ok.get(c, failed) > failed]:
        del whole[connection_id]
    return whole, paid


def candidates(*, needs_images: bool = False, now: datetime | None = None) -> list[Candidate]:
    """Everything Auto may pick, best first. Deterministic: the same models, the same
    outcomes and the same clock always give the same order."""
    now = now or datetime.now(timezone.utc)
    outcomes = store.list_outcomes()
    whole, paid = _holds(outcomes, now)
    answered = store.recent_answers()

    by_connection: dict[str, list[store.Model]] = {}
    copies: dict[str, int] = {}
    for model in store.list_models():
        by_connection.setdefault(model.provider_id, []).append(model)
        copies[model.model_id] = copies.get(model.model_id, 0) + 1

    ranked: list[tuple[tuple, Candidate]] = []
    for connection in store.list_connections():
        if lacks_key(connection):
            continue
        connection_models = by_connection.get(connection.id, [])
        routers = [m for m in connection_models if (m.facts or {}).get("router")]
        for model in (routers or connection_models):
            if not fits(model, needs_images=needs_images):
                continue
            outcome = outcomes.get((connection.id, model.model_id))
            worked = _moment(outcome.last_ok_at) if outcome else None
            # A model that answered in the saved conversation has worked here even if no
            # outcome was recorded then — counted only when its id is on ONE connection,
            # so the credit can't land on the wrong one.
            if worked is None and copies.get(model.model_id) == 1:
                worked = _moment(answered.get(model.model_id))
            held = connection.id in whole or (
                connection.id in paid and (model.facts or {}).get("free") is False
                and not (worked and worked > paid[connection.id]))
            key = (
                1 if (held or waiting(outcome, now)) else 0,                       # steered around last
                0 if worked else 1,                                                # proven before untried
                speed_class(outcome.ttft_ms if outcome else None) if worked else 1,  # quicker first
                -(worked.timestamp()) if worked else 0.0,                          # most recent success
                0 if (model.facts or {}).get("tools") is True else 1,             # reported tool-capable
                connection.created_at, model.added_at, model.model_id,            # then set-up order
            )
            ranked.append((key, Candidate(connection, model, proven=worked is not None)))
    ranked.sort(key=lambda pair: pair[0])
    return [candidate for _, candidate in ranked]
