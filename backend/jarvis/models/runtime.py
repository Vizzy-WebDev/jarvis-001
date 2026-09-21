"""What both callers — the turn loop's client and the one-shot `ask` — do around a
provider call, so neither carries its own copy."""

from __future__ import annotations

import logging

from ..ai import NoModelAvailable
from ..events import EventType, bus
from .errors import ProviderError
from .selection import Resolved
from .types import Usage

logger = logging.getLogger(__name__)

#: Roles whose calls run with nobody waiting on them.
BACKGROUND_ROLES = {"background", "utility"}


def name_of(resolved: Resolved) -> str:
    return f"{resolved.model.label or resolved.model.model_id} on {resolved.connection.label}"


#: Said when a model the person NAMED fails: Jarvis did not swap it, and here is the
#: way to let it, in words rather than a setting they would have to go looking for.
_STAYED = (" Jarvis stays on the model you picked. Choose Auto in the model list if you'd "
           "rather it work around problems like this.")


def failure(resolved: Resolved, err: ProviderError, *, named: bool = False) -> NoModelAvailable:
    """A provider's refusal, as the plain 'no model could answer' the turn loop
    already knows how to say. The provider's own words are kept in it.

    `named` is True when the person picked this model themselves, and adds that
    Jarvis kept to it — the one place a failure explains what Auto would change."""
    return NoModelAvailable(
        f"{name_of(resolved)} couldn't answer. {err}" + (_STAYED if named else ""),
        detail={"reason": "provider_error", "provider": resolved.connection.label,
                "model": resolved.model.model_id, "kind": err.kind, "status": err.status},
    )


def brief(err: ProviderError, limit: int = 160) -> str:
    """One line of what went wrong, for saying why Auto moved on."""
    text = " ".join(str(err).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def all_failed(failures: list[tuple[Resolved, ProviderError]]) -> NoModelAvailable:
    """Auto tried every model it was going to and none could answer — each one named
    with its own reason, so nothing has to be guessed at."""
    from . import store

    lines = "; ".join(f"{name_of(r)}: {brief(e, 140)}" for r, e in failures)
    never = not any(o.last_ok_at for o in store.list_outcomes().values())
    hint = (" Auto hasn't seen any model work here yet. Pick one model yourself once — when it answers, "
            "Auto remembers it worked and starts there.") if never else ""
    return NoModelAvailable(
        f"Auto tried {len(failures)} model{'s' if len(failures) != 1 else ''} and none could answer. "
        f"{lines}{hint}",
        detail={"reason": "auto_exhausted",
                "attempts": [{"provider": r.connection.label, "model": r.model.model_id,
                              "kind": e.kind, "status": e.status} for r, e in failures]},
    )


def record(resolved: Resolved, err: ProviderError | None) -> None:
    """Remember how a real call went, for Auto to read. Never allowed to break the call."""
    from . import store

    try:
        if err is None:
            store.record_success(resolved.connection.id, resolved.model.model_id)
        else:
            store.record_failure(resolved.connection.id, resolved.model.model_id,
                                 kind=err.kind, status=err.status, message=str(err))
    except Exception:  # noqa: BLE001 - bookkeeping must never cost anyone their answer
        logger.exception("couldn't record a model outcome")


def same_model(requested: str, reported: str | None) -> bool:
    """Did the answer come from the model that was asked for?

    A provider routinely reports a more specific name than the one requested — an
    alias resolving to a dated snapshot, a local tag — so either being the start of
    the other counts. Anything else is a different model, and is said so.
    """
    if not reported:
        return True  # it did not say; that is not a mismatch
    a, b = requested.lower().removeprefix("models/"), reported.lower().removeprefix("models/")
    return a == b or a.startswith(b) or b.startswith(a)


def publish_completed(resolved: Resolved, *, session_id: str | None, reported: str | None,
                      usage: Usage | None, background: bool) -> None:
    """Tell the cost ledger what a call really used, in the shape it reads.

    Only what the provider reported: a call that reported nothing publishes
    nothing, rather than a zero standing in for "not measured".
    """
    if not usage:
        return
    units = {"unitsIn": usage.tokens_in, "unitsOut": usage.tokens_out, "cachedIn": usage.cached_in}
    units = {k: v for k, v in units.items() if v is not None}
    if not units:
        return
    bus.publish(EventType.MODEL_CALL_COMPLETED, {
        "sessionId": session_id,
        "provider": resolved.connection.kind,
        "model": reported or resolved.model.model_id,
        "modelId": reported or resolved.model.model_id,
        "background": background,
        "usage": units,
    })
