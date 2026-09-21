"""What both callers — the turn loop's client and the one-shot `ask` — do around a
provider call, so neither carries its own copy."""

from __future__ import annotations

from ..ai import NoModelAvailable
from ..events import EventType, bus
from .errors import ProviderError
from .selection import Resolved
from .types import Usage

#: Roles whose calls run with nobody waiting on them.
BACKGROUND_ROLES = {"background", "utility"}


def name_of(resolved: Resolved) -> str:
    return f"{resolved.model.label or resolved.model.model_id} on {resolved.connection.label}"


def failure(resolved: Resolved, err: ProviderError) -> NoModelAvailable:
    """A provider's refusal, as the plain 'no model could answer' the turn loop
    already knows how to say. The provider's own words are kept in it."""
    return NoModelAvailable(
        f"{name_of(resolved)} couldn't answer. {err}",
        detail={"reason": "provider_error", "provider": resolved.connection.label,
                "model": resolved.model.model_id, "kind": err.kind, "status": err.status},
    )


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
