"""How Auto chooses — and the promise that it never touches an explicit choice.

Auto is one of the person's two ways to pick a model. The other is naming one, and
that is honoured exactly: this module is not consulted for it at all.

When Auto is chosen, Jarvis picks per turn from the models that are set up now,
by rules that are written down here and never involve chance:

1. **Who can be picked.** Every model on the person's list, on a connection that can
   be used (it has its key, where one is needed), except one the PROVIDER has itself
   said cannot take a Jarvis turn — a video model, one that doesn't produce text, one
   that says tool calling doesn't work — and, when the turn carries a picture, one
   that said it doesn't accept images; when the request asks for reasoning, one not
   reported to reason. Nothing is excluded on a guess from its name: a provider that
   reports nothing about a model leaves it in. On a connection set to prefer its
   routers (`prefer_routers`), when some of its models are the gateway's own routers
   (`facts["router"]`, as that gateway's module read it), only those are candidates
   there: a router already picks and falls back across the rest of that connection's
   models on its own, so Auto does not also walk them individually.
2. **Who is preferred.** Models that have answered here before come first — from the
   outcomes recorded, or from the replies already saved in the conversation. Among
   them, quicker ones first (in coarse classes, so Auto stays steady rather than
   restless), then the one that answered most recently. Untried models follow, those
   the provider reported as tool-capable ahead of those it said nothing about, then
   in the order they were connected and listed. **Speed is only ever a preference:**
   a slow model is never excluded, never counted as failing for being slow, and is
   used whenever the quicker ones aren't available.
3. **Who is steered around.** Anything a recent failure is holding back
   (`health.py`): the one model, its whole credential or its whole connection — as far
   as the provider module that read the failure said it reached — for as long as the
   provider said to wait, or 5 minutes doubling per failure in a row (capped at 2
   hours). Any answer lifts it. A "needs credit" hold passes over models the provider
   lists as free. Moved down, never removed: if nothing else is left it is still tried.
   Nothing here decides what a failure means — that was decided where it was read.

What Auto does with that list is in `attempt.py`: try the first and, only if it
FAILS before saying anything, the next — never because a model is merely slow.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .. import config
from . import health, kinds, store

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


def fits(model: store.Model, *, needs_images: bool = False, needs_reasoning: bool = False) -> bool:
    """Can this model take a turn, going only by what its provider said about it?

    Reasoning is the one thing that must be REPORTED rather than merely not denied:
    a request that asks for it goes only to a model whose provider said it reasons."""
    facts = model.facts or {}
    if facts.get("chat") is False or facts.get("tools") is False:
        return False
    if needs_images and facts.get("image") is False:
        return False
    if needs_reasoning and (facts.get("reasoning") or {}).get("supported") is not True:
        return False
    return True


def _moment(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def speed_class(ttft_ms: int | None) -> int:
    """0 quick, 1 middling (or not measured), 2 slow. Coarse on purpose."""
    if ttft_ms is None:
        return 1
    return 0 if ttft_ms <= FAST_MS else 1 if ttft_ms <= OK_MS else 2


def candidates(*, needs_images: bool = False, needs_reasoning: bool = False,
               now: datetime | None = None) -> list[Candidate]:
    """Everything Auto may pick, best first. Deterministic: the same models, the same
    outcomes, the same holds and the same clock always give the same order."""
    now = now or datetime.now(timezone.utc)
    outcomes = store.list_outcomes()
    answered = store.recent_answers()
    listed = store.list_models()  # the catalog with the person's own decisions laid over it
    holds = health.Holds.load(now, listed)

    by_connection: dict[str, list[store.Model]] = {}
    copies: dict[str, int] = {}
    for model in listed:
        by_connection.setdefault(model.provider_id, []).append(model)
        copies[model.model_id] = copies.get(model.model_id, 0) + 1

    ranked: list[tuple[tuple, Candidate]] = []
    for connection in store.list_connections():
        if lacks_key(connection):
            continue
        connection_models = by_connection.get(connection.id, [])
        routers = ([m for m in connection_models if (m.facts or {}).get("router") is True]
                   if connection.prefer_routers else [])
        for model in (routers or connection_models):
            if not fits(model, needs_images=needs_images, needs_reasoning=needs_reasoning):
                continue
            outcome = outcomes.get((connection.id, model.model_id))
            worked = _moment(outcome.last_ok_at) if outcome else None
            # A model that answered in the saved conversation has worked here even if no
            # outcome was recorded then — counted only when its id is on ONE connection,
            # so the credit can't land on the wrong one.
            if worked is None and copies.get(model.model_id) == 1:
                worked = _moment(answered.get(model.model_id))
            held = holds.blocking(connection, model) is not None
            key = (
                1 if held else 0,                                                  # steered around last
                0 if worked else 1,                                                # proven before untried
                speed_class(outcome.ttft_ms if outcome else None) if worked else 1,  # quicker first
                -(worked.timestamp()) if worked else 0.0,                          # most recent success
                0 if (model.facts or {}).get("tools") is True else 1,             # reported tool-capable
                connection.created_at,                                             # then set-up order:
                model.user_order if model.user_order is not None else float("inf"),  # the person's own,
                model.added_at, model.model_id,                                    # else as listed
            )
            ranked.append((key, Candidate(connection, model, proven=worked is not None)))
    ranked.sort(key=lambda pair: pair[0])
    return [candidate for _, candidate in ranked]
