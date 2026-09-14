"""Turning "think this hard" into something a specific model will accept.

The catalog says which levels a version offers; this decides what actually gets
sent for a given request, and remembers when a model turns out not to accept
the parameter at all.

**Clamping is the normal case, not an error path.** The levels genuinely differ
between providers — read out of the installed SDKs, the OpenAI-shaped wire
accepts a value above `high` and Gemini's level enum does not — so the moment a
person sets one preference and Jarvis falls back across two providers, a
request will outrun what the answering model can do. Failing the turn there
would be hostile: the user asked for a good answer, not for a lecture about
enum ranges. So the request is lowered to what this model can take and the fact
is reported, which matches how the gateway already treats a pin it cannot
honour.

**Clamping goes DOWN, never up.** Given levels (low, high) and a request for
medium, this sends low. Never spending more than was asked for is the
predictable rule and the one that cannot surprise someone on a metered key; the
alternative silently bills more than the person chose.

**A refusal is remembered so it is paid for once.** When a provider rejects the
parameter, the adapter retries without it and records that here. Without the
record, every turn would pay a failed request plus a retry, forever, on exactly
the rosters that are already rate-limited. This is the same learn-from-reality
shape `availability.py` uses for health, kept in its own file for the same
reason availability is not kept in `models.json`: one is a fact about whether a
model works, the other is a fact about what it accepts, and they change on
completely different clocks.
"""

from __future__ import annotations

import threading
import time
import logging
from typing import Any, Iterator

from ..redact import redact_text
from ..store import read_json, write_json
from ..catalog import Effort, EffortRequest, EffortScheme

logger = logging.getLogger(__name__)

FILE = "model-effort"


def clamp(requested: Effort, scheme: EffortScheme) -> tuple[Effort | None, bool]:
    """The nearest level this scheme can actually serve, and whether it moved.

    `None` means there is nothing to send — either the scheme offers no control
    at all, or nobody has established that it does. Pure: no store, no I/O, so
    the interesting decision is testable on its own.
    """
    if not scheme.controllable or not scheme.levels:
        return None, False
    if scheme.supports(requested):
        return requested, False
    # The highest level at or below what was asked for; the floor when the
    # request sits beneath everything on offer.
    below = [level for level in scheme.levels if level <= requested]
    chosen = below[-1] if below else scheme.floor
    return chosen, chosen is not requested


def plan(
    requested: Effort | None,
    scheme: EffortScheme,
    *,
    provider: str,
    model: str,
) -> EffortRequest | None:
    """What to send for this call, or None to send nothing.

    None covers every reason not to ask: the version declares no reasoning
    control, nobody has established whether it has any, or it has already
    refused the parameter once and there is no point paying for that again.
    """
    if is_unsupported(provider, model):
        return None
    target = requested if requested is not None else scheme.default
    if target is None:
        return None
    level, was_clamped = clamp(target, scheme)
    if level is None:
        return None
    return EffortRequest(level=level, scheme=scheme, requested=target, clamped=was_clamped)


# --- what a model has refused ------------------------------------------------
#
# Cached and written through, the same shape as availability.py: read on every
# call, written rarely, and a file rather than memory because the point is to
# not re-learn it after a restart.

_lock = threading.RLock()
_cache: dict[str, dict[str, Any]] | None = None


def _key(provider: str | None, model: str | None) -> str:
    return f"{provider or 'unknown'}/{model or ''}"


def _load() -> dict[str, dict[str, Any]]:
    global _cache
    with _lock:
        if _cache is None:
            stored = read_json(FILE, {}) or {}
            _cache = stored.get("versions", {}) if isinstance(stored, dict) else {}
        return _cache


def _flush() -> None:
    """Write through. Called with the lock held."""
    write_json(FILE, {"versions": _cache or {}})


def mark_unsupported(provider: str | None, model: str | None,
                     detail: str | None = None) -> None:
    """Record that this version rejected the effort parameter.

    `detail` is redacted here rather than by the caller, for the same reason
    availability does it: a provider's error can quote the key it was sent, and
    this file outlives the process that wrote it.
    """
    if not model:
        return
    with _lock:
        versions = _load()
        versions[_key(provider, model)] = {
            "effort": "unsupported",
            "detail": redact_text(detail),
            "since": int(time.time() * 1000),
        }
        _flush()


def is_unsupported(provider: str | None, model: str | None) -> bool:
    if not model:
        return False
    return _load().get(_key(provider, model), {}).get("effort") == "unsupported"


def clear(provider: str | None, model: str | None) -> None:
    """Forget a refusal — a model edited, or a user asking to try again.

    Permanent until something clears it, deliberately: unlike a rate limit,
    "this model does not take that parameter" does not heal on a timer, and a
    cooldown would mean re-paying for the same failed call every few hours.
    """
    with _lock:
        versions = _load()
        if versions.pop(_key(provider, model), None) is not None:
            _flush()


def reset_for_tests() -> None:
    global _cache
    with _lock:
        _cache = None


# --- driving one call, and surviving a refusal -------------------------------

def call_with_effort(
    adapter: Any,
    entry: dict[str, Any],
    messages: list[dict[str, Any]],
    *,
    system: str = "",
    tools: list[dict[str, Any]] | None = None,
    effort: EffortRequest | None = None,
    provider: str | None = None,
    model: str | None = None,
    on_first_token: Any = None,
) -> Iterator[Any]:
    """Stream from an adapter, dropping the effort parameter if it is refused.

    Lives on the gateway side rather than inside each adapter for the reason
    the adapters already give for setting `max_retries=0` on every SDK: retry
    policy belongs in one place, not copied into three wire formats that will
    drift. It also keeps the dependency running one way — an adapter cannot
    import this, because the gateway imports adapters.

    **The retry only happens if nothing was streamed.** A rejected parameter is
    a 400 before any tokens exist, so in practice the guard never fires; it is
    here because the cost of being wrong is the user seeing the first half of an
    answer twice, and a guard that never triggers is cheaper than that.

    The refusal is recorded, so the failed round trip is paid once rather than
    on every turn for the life of the install.

    `on_first_token(elapsed_ms)` is called once per ATTEMPT, with the clock
    restarted for the retry. The timing lives here rather than at the call site
    because this is the only layer that knows a retry happened: measured from
    outside, a refused first attempt would be added to the successful one and
    recorded as the model being slow, which is the opposite of what happened.
    """
    from .error_kind import refused_parameter

    # A one-slot list rather than a local: `_timed` is what actually pulls from
    # the adapter, so it is the only thing that can know anything was streamed.
    produced = [False]
    try:
        yield from _timed(adapter.stream(entry, messages, system=system, tools=tools,
                                         effort=effort), on_first_token, produced)
        return
    except Exception as err:  # noqa: BLE001 — the classifier decides what this was
        name = refused_parameter(err)
        if effort is None or name is None or produced[0]:
            raise
        logger.info("%s refused %s; retrying without it", model or "model", name)
        mark_unsupported(provider, model, detail=str(err))

    # Outside the except block: a failure in the retry should surface as itself
    # rather than chained to an error we already decided was not the model's
    # fault, which is what a reader of the log would otherwise be handed.
    yield from _timed(adapter.stream(entry, messages, system=system, tools=tools,
                                     effort=None), on_first_token, produced)


def _timed(events: Iterator[Any], on_first_token: Any, produced: list[bool]) -> Iterator[Any]:
    """Pass events through, reporting how long the first one took to arrive."""
    started = time.monotonic()
    first = True
    for event in events:
        if first and on_first_token is not None:
            on_first_token((time.monotonic() - started) * 1000)
        first = False
        produced[0] = True
        yield event
