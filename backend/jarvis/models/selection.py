"""Which model is selected, and whether that choice can be run right now.

The person chooses in one of two ways. They can NAME a model, and then that exact
model runs every turn — no other is ever used in its place. Or they can choose Auto,
and then Jarvis picks per turn from the models that are set up (`auto.py` says how).
Auto is the person's own choice, and it is the only thing that lets more than one
model be tried for a turn.

Three separate things, never run together:

* **Selected** is the person's own choice, held as preferences. Only they change it.
* **Available** is whether that choice currently resolves to something runnable.
  It is worked out on demand and never stored as if it were a fact about the choice.
* **Executed** is whether a request really ran there — known only from the
  provider's answer, and not this module's business.

The rule that follows: an unavailable selection is REPORTED, in words, and stays
selected. Nothing here ever picks another model on the person's behalf — a
different model answering under the name of the one they chose is worse than an
honest "that isn't working".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .. import config, prefs
from ..ai import NoModelAvailable
from . import auto, kinds, store
from .types import Target

#: The one thing a plain connection cannot do at all, and so the one thing to
#: refuse up front rather than answer from memory as though it had been done.
_UNSUPPORTED_NEEDS = {
    "webSearch": "Searching the web through the model isn't something this connection can do, "
                 "so it wasn't done.",
}


@dataclass(frozen=True)
class Resolved:
    connection: store.Connection
    model: store.Model
    target: Target
    effort: str | None
    #: Auto only: this model has answered here before (recorded, or in the saved chat).
    proven: bool = False


@dataclass(frozen=True)
class Plan:
    """What one model call may run on. `auto` is False for a named model, and then
    `attempts` is exactly one entry — the only model that will be used."""

    auto: bool
    attempts: list[Resolved]


@dataclass(frozen=True)
class Availability:
    #: 'ok' | 'none' | 'missing_connection' | 'missing_model' | 'no_key'
    state: str
    message: str | None


def chosen() -> tuple[str | None, str | None, str | None]:
    """The stored selection, exactly as the person left it."""
    saved = prefs.get_prefs()
    return saved.get("selectedProviderId"), saved.get("selectedModelId"), saved.get("selectedEffort")


def effort_levels(model: store.Model | None) -> list[str]:
    """The levels the PROVIDER reported for this model — empty when it reported none."""
    if not model:
        return []
    return list(((model.facts or {}).get("effort") or {}).get("levels") or [])


def target_for(connection: store.Connection) -> Target:
    """Where a request to this connection goes, and the key it carries."""
    key = config.get_secret(connection.secret_ref) if connection.secret_ref else None
    return Target(base_url=kinds.base_url_for(connection.kind, connection.base_url), api_key=key)


def is_auto() -> bool:
    """Has the person chosen Auto, rather than naming a model?"""
    return bool(prefs.get_prefs().get("selectedAuto"))


def _why_not(connection: store.Connection | None, model: store.Model | None, model_id: str) -> Availability:
    if connection is None:
        return Availability("missing_connection",
                            f"The model you picked ({model_id}) came from a connection that has been removed. "
                            "Choose a model on the Model Settings screen.")
    if model is None:
        return Availability("missing_model",
                            f"The model you picked ({model_id}) is no longer set up on {connection.label}. "
                            "Choose a model on the Model Settings screen.")
    if auto.lacks_key(connection):
        return Availability("no_key", f"{connection.label} has no key saved. "
                                      "Add one on the Model Settings screen.")
    return Availability("ok", None)


def availability() -> Availability:
    if is_auto():
        if auto.candidates():
            return Availability("ok", None)
        return Availability("none", "Auto has no model to choose from yet. Connect a provider on "
                                    "the Model Settings screen.")
    provider_id, model_id, _ = chosen()
    if not provider_id or not model_id:
        return Availability("none", "No model is selected yet. Connect a provider and choose a model "
                                    "on the Model Settings screen.")
    return _why_not(store.get_connection(provider_id), store.get_model(provider_id, model_id), model_id)


def _pinned(pin: str) -> tuple[store.Connection, store.Model]:
    """A one-off request for a specific model id (a scheduled task naming one).

    Found or refused — never approximated. If the id is on several connections and
    none is the selected one, that is ambiguous, and ambiguity is an error too.
    """
    matches = [m for m in store.list_models() if m.model_id == pin]
    selected_provider, _, _ = chosen()
    for match in matches:
        if match.provider_id == selected_provider:
            matches = [match]
            break
    if not matches:
        raise NoModelAvailable(f"The model this asked for ({pin}) isn't set up on any connection.",
                               detail={"reason": "missing_model"})
    if len(matches) > 1:
        raise NoModelAvailable(f"The model this asked for ({pin}) is set up on more than one connection, "
                               "so Jarvis can't tell which you meant.", detail={"reason": "ambiguous"})
    connection = store.get_connection(matches[0].provider_id)
    assert connection is not None
    return connection, matches[0]


def resolve(pin: str | None = None) -> Resolved:
    """What to run a NAMED model on, or `NoModelAvailable` saying why not. Under Auto
    there is no single answer; a caller that can be under Auto uses `plan`."""
    effort: str | None = None
    if pin:
        connection, model = _pinned(pin)
        problem = _why_not(connection, model, pin)
    else:
        provider_id, model_id, effort = chosen()
        problem = availability()
        connection = store.get_connection(provider_id) if provider_id else None
        model = store.get_model(provider_id, model_id) if provider_id and model_id else None
    if problem.state != "ok" or connection is None or model is None:
        raise NoModelAvailable(problem.message or "No model is available.", detail={"reason": problem.state})

    # Only ever a level this model was reported to accept. A pinned model is not
    # the selected one, so the selected model's effort is not carried over to it.
    if effort not in effort_levels(model):
        effort = None
    return Resolved(connection=connection, model=model, target=target_for(connection), effort=effort)


def plan(pin: str | None = None, *, needs_images: bool = False) -> Plan:
    """The models a call may run on, in the order to try them.

    A named model — the person's selection, or a pin — is a plan of exactly one and
    is refused, in words, if it can't be run. Only Auto yields several.
    """
    if pin or not is_auto():
        return Plan(auto=False, attempts=[resolve(pin)])
    picks = auto.candidates(needs_images=needs_images)
    if not picks:
        raise NoModelAvailable(
            "Auto has no model to choose from" + (" for a message with a picture in it" if needs_images else "")
            + ". Connect a provider on the Model Settings screen.",
            detail={"reason": "none"})
    # One target per CONNECTION, not per candidate: a target reads the saved key from disk,
    # and there can be hundreds of candidates on a handful of connections.
    targets: dict[str, Target] = {}
    for c in picks:
        if c.connection.id not in targets:
            targets[c.connection.id] = target_for(c.connection)
    return Plan(auto=True, attempts=[
        Resolved(connection=c.connection, model=c.model, target=targets[c.connection.id], effort=None,
                 proven=c.proven)
        for c in picks])


def ready() -> list[auto.Candidate]:
    """What could answer right now: the named model when it can be run, or what Auto
    would choose from. Read from what is stored; no provider is called."""
    if is_auto():
        return auto.candidates()
    provider_id, model_id, _ = chosen()
    if availability().state != "ok" or not provider_id or not model_id:
        return []
    connection, model = store.get_connection(provider_id), store.get_model(provider_id, model_id)
    return [auto.Candidate(connection, model)] if connection and model else []


def check_needs(need: dict[str, bool] | None) -> None:
    for name, wanted in (need or {}).items():
        if wanted and name in _UNSUPPORTED_NEEDS:
            raise NoModelAvailable(_UNSUPPORTED_NEEDS[name], detail={"reason": "unsupported_need", "need": name})


def set_selection(provider_id: str, model_id: str, effort: str | None) -> dict[str, Any]:
    """Record a choice. It must be a model that exists now; an effort the model
    was not reported to accept is dropped, and the stored value is what comes back."""
    connection = store.get_connection(provider_id)
    model = store.get_model(provider_id, model_id) if connection else None
    if connection is None or model is None:
        raise LookupError("That model isn't set up.")
    if effort not in effort_levels(model):
        effort = None
    prefs.set_prefs({"selectedAuto": False, "selectedProviderId": provider_id,
                     "selectedModelId": model_id, "selectedEffort": effort})
    return {"selectedProviderId": provider_id, "selectedModelId": model_id, "selectedEffort": effort}


def set_auto() -> None:
    """Let Jarvis choose. The named model is forgotten, and so is its effort: effort
    belongs to a particular model, and under Auto there is no particular model."""
    prefs.set_prefs({"selectedAuto": True, "selectedProviderId": None,
                     "selectedModelId": None, "selectedEffort": None})
