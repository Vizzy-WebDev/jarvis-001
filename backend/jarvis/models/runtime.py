"""What both callers — the turn loop's client and the one-shot `ask` — do around a
provider call, so neither carries its own copy."""

from __future__ import annotations

import logging
import re

from ..ai import NoModelAvailable
from ..events import EventType, bus
from . import gateways
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
                "model": resolved.model.model_id, "kind": err.kind, "scope": err.scope, "status": err.status},
    )


def cooling_down(resolved: Resolved, seconds: int, why: str | None) -> NoModelAvailable:
    """A model the person NAMED, not tried because a recent failure is still holding it
    back — said as exactly that, with when it will be tried again and the one way round
    it. Nothing is substituted for it."""
    reason = f" ({brief_text(why, 140)})" if why else ""
    return NoModelAvailable(
        f"That model failed recently and is cooling down. Next attempt in {seconds}s. Or switch to Auto."
        f"{reason}",
        detail={"reason": "cooling_down", "provider": resolved.connection.label,
                "model": resolved.model.model_id, "retryInS": seconds},
    )


def brief_text(text: str, limit: int = 160) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def brief(err: ProviderError, limit: int = 160) -> str:
    """One line of what went wrong, for saying why Auto moved on."""
    return brief_text(str(err), limit)


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
                              "kind": e.kind, "scope": e.scope, "status": e.status} for r, e in failures]},
    )


def record(resolved: Resolved, err: ProviderError | None, *, ms: int | None = None) -> None:
    """Remember how a real call went — the outcome for Auto's preferences, and the hold
    a failure puts on whatever it reached (`health.py`). Never allowed to break the
    call. `ms` is how long it took to start answering, for a call that succeeded."""
    from . import health, store

    try:
        if err is None:
            store.record_success(resolved.connection.id, resolved.model.model_id, ms)
            health.clear_on_success(resolved.connection, resolved.model.model_id)
        else:
            store.record_failure(resolved.connection.id, resolved.model.model_id,
                                 kind=err.kind, status=err.status, message=str(err))
            health.record(resolved.connection, resolved.model.model_id, err)
    except Exception:  # noqa: BLE001 - bookkeeping must never cost anyone their answer
        logger.exception("couldn't record a model outcome")


#: A dated snapshot of an alias: "-2025-09-29", "-20250929", or a local ":latest" tag.
_SNAPSHOT = re.compile(r"^(?:-\d{4}-\d{2}-\d{2}|-\d{8}|:latest)$")


def _names(model_id: str) -> tuple[str, str]:
    full = model_id.lower().removeprefix("models/")
    return full, full.rsplit("/", 1)[-1]


def same_model(requested: str, reported: str | None, gateway_kind: str | None = None) -> bool:
    """Did the answer come from the model that was asked for?

    Only these count as the same model — nothing looser, so `gpt-4` answered by
    `gpt-4o` is reported as the different model it is:

    * the same id;
    * the same id once a namespace is set aside — a gateway's own prefix
      ("no-think/cc/") is part of ITS name for the model, not the model's;
    * the requested alias plus a dated-snapshot tail (`claude-x` answered as
      `claude-x-20250929`, `gpt-4o` as `gpt-4o-2024-08-06`, `llama3` as `llama3:latest`);
    * a pairing the connection's declared gateway lists (`gateways.ALIASES`).
    """
    if not reported:
        return True  # it did not say; that is not a mismatch
    (a_full, a), (b_full, b) = _names(requested), _names(reported)
    if a_full == b_full or a == b:
        return True
    if b.startswith(a) and _SNAPSHOT.match(b[len(a):]):
        return True
    return gateways.is_alias(gateway_kind, a_full, b_full)


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
