"""Running one model call, and — only under Auto — trying the next model if it fails.

Both callers (the turn loop's client and the one-shot `ask`) run a `selection.Plan`
through `run`, so the rules are in one place. None of them decides what a failure
MEANS: the provider module that read it said how far it reaches (`ProviderError.scope`)
and whether trying again could help (`retryable_elsewhere`), and this only acts on that.

* **A request that is wrong wherever it goes is not taken anywhere else.** A failure
  whose scope is `request` ends the call at once, in the provider's own words: no other
  model is tried (it would be refused the same way, and spend the person's allowance
  finding that out), no retry, and nothing is held against the model.
* **A named model is a plan of one, and there is no second.** If it fails, the call
  fails, in the provider's own words. Before it is tried at all, it is refused — said
  as exactly that — only while the provider itself asked for a wait that hasn't passed,
  or while its whole key or connection is held (refused key, no credit, unreachable).
* **Auto walks its list**, best first, and moves on ONLY when a model has actually
  failed. It never moves on because a model is slow: a request the provider has
  accepted is left to finish however long it thinks, and once a word has gone out no
  other model can take over the sentence, so a failure after that is reported as it is.
* **The same model is asked again** only when the provider module said a later attempt
  could work (`retryable_elsewhere`) and there is nowhere else to go — after a short
  pause, or the provider's own stated wait if that is short; a long stated wait is not
  sat through.
* **What a failure reaches is left alone for the rest of the step**: the whole
  connection (`provider`), everything on the same key (`credential`), or — for one
  model (`model`, `unknown`) — the model, with two such failures on one connection
  leaving that connection too. "Needs credit" passes over models listed as free. After
  a failure, once `auto.FIND_BUDGET_S` has passed, no model that has never worked here
  is started — that gates STARTING attempts only and never touches one in progress.
* **Nothing is silent.** A move is announced (`Moved`), naming what failed and why,
  and if every model fails they are all named. Every real call's outcome is recorded,
  with the hold it puts on what it reached (`health.py`).

This module imports no orchestrator and nothing that does, so a tool asking a
question through `ai.ask` can never be led into the turn loop by way of it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from typing import Any, Generator

from ..ai import NoModelAvailable
from . import auto, health, providers, runtime
from .errors import ProviderError
from .request import ChatRequest
from .selection import Plan, Resolved
from .types import Finished, TextDelta

logger = logging.getLogger(__name__)

#: How many more times the same model is asked after a failure that could pass, and
#: the pause before each — only when there is nowhere else to go.
_RETRIES = 2
_RETRY_WAITS_S = (1.0, 3.0)
#: A provider-stated wait longer than this is not sat through: reporting it (or going
#: elsewhere) beats a long silence with nothing said.
_MAX_STATED_WAIT_S = 10.0

#: What a provider module can do with a reply it wasn't expecting. These become the
#: same plain "couldn't read the reply" as any other, rather than a crash.
_MALFORMED = (KeyError, TypeError, ValueError, IndexError, AttributeError)

#: The clock and the pause, named so a test can drive time instead of waiting for it.
_clock = time.monotonic
_sleep = time.sleep


@dataclass(frozen=True)
class Moved:
    """Auto is about to answer from a different model than the one it tried first."""

    to_model_id: str
    from_model_id: str
    reason: str


@dataclass(frozen=True)
class Result:
    resolved: Resolved
    finished: Finished


def _events(resolved: Resolved, request: ChatRequest) -> Generator[Any, None, None]:
    try:
        provider = providers.for_format(resolved.connection.format)
        reasoning = resolved.reasoning if resolved.reasoning is not None else request.options.reasoning
        ask = replace(request, model_id=resolved.model.model_id,
                      options=replace(request.options, reasoning=reasoning))
        yield from provider.stream(resolved.target, ask, facts=resolved.model.facts)
    except ProviderError:
        raise
    except _MALFORMED as err:
        logger.exception("a provider sent a reply that could not be read")
        raise ProviderError(f"{resolved.connection.label} sent part of its reply in a form Jarvis "
                            "couldn't read.", kind="reply", scope="model") from err


def _pause_before_again(err: ProviderError, tries: int) -> float | None:
    """How long to wait before asking the same model again — or None: don't."""
    if err.scope == "request" or not err.retryable_elsewhere:
        return None
    wait = _RETRY_WAITS_S[min(tries, len(_RETRY_WAITS_S) - 1)]
    if err.retry_after_s is not None:
        if err.retry_after_s > _MAX_STATED_WAIT_S:
            return None
        wait = max(wait, err.retry_after_s)
    return wait


def _next(queue: list[Resolved], failures: list[tuple[Resolved, ProviderError]]) -> Resolved:
    """The next to try: a different connection than any that has just failed, if there is one."""
    failed = {r.connection.id for r, _ in failures}
    for i, candidate in enumerate(queue):
        if candidate.connection.id not in failed:
            return queue.pop(i)
    return queue.pop(0)


def _reached(candidate: Resolved, failed: Resolved, scope: str) -> bool:
    """Is `candidate` within what a failure of `failed` at `scope` reached?"""
    if scope == "provider":
        return candidate.connection.id == failed.connection.id
    if scope == "credential":
        return candidate.connection.credential_id == failed.connection.credential_id
    return (candidate.connection.credential_id == failed.connection.credential_id
            and candidate.model.model_id == failed.model.model_id)


def _prune(queue: list[Resolved], failed: Resolved, err: ProviderError, strikes: dict[str, int],
           started: float, priced: set[str]) -> None:
    """After a failure, drop from the rest of this step whatever the failure reached."""
    connection_id = failed.connection.id
    strikes[connection_id] = strikes.get(connection_id, 0) + 1
    scope = health.held_scope(err) or "model"
    if err.kind == "billing":  # the account needs credit: models listed as free carry on
        queue[:] = [r for r in queue if not (_reached(r, failed, scope)
                                             and not health.billing_exempt(r.model, r.connection.id in priced))]
    elif scope in health.WIDE:
        queue[:] = [r for r in queue if not _reached(r, failed, scope)]
    if strikes[connection_id] >= auto.STRIKES_PER_CONNECTION:
        queue[:] = [r for r in queue if r.connection.id != connection_id]
    if _clock() - started > auto.FIND_BUDGET_S:  # stop STARTING unknowns; never cut one in progress
        queue[:] = [r for r in queue if r.proven]


def _skipped(failures: list[tuple[Resolved, ProviderError]]) -> str:
    shown = "; ".join(f"{runtime.name_of(r)} ({runtime.brief(e, 110)})" for r, e in failures[:3])
    more = len(failures) - 3
    return shown + (f"; and {more} more" if more > 0 else "")


def _refuse_if_held(resolved: Resolved) -> None:
    """A named model is not tried while a firm hold is on it — and is never swapped."""
    holds = health.Holds.load()
    hold = holds.refusing(resolved.connection, resolved.model)
    if hold is not None:
        seconds = holds.seconds_left(hold)
        logger.info("model %s on %s not tried: held (%s scope, %s) for %ss more",
                    resolved.model.model_id, resolved.connection.label, hold.scope, hold.kind, seconds)
        raise runtime.cooling_down(resolved, seconds, hold.message)


def run(plan: Plan, *, request: ChatRequest, named: bool = True) -> Generator[TextDelta | Moved, None, Result]:
    """Yield what the model says as it says it; return which model answered and how.

    `request` is the typed request (`conversation.to_chat_request`); each attempt gets
    it with its own model id and reasoning level filled in. `named` is True when the
    person picked the model themselves (not a pin from a scheduled task), which is
    when a failure says Jarvis stayed on it on purpose. Raises `NoModelAvailable` in
    words when nothing could answer."""
    queue = list(plan.attempts)
    failures: list[tuple[Resolved, ProviderError]] = []
    strikes: dict[str, int] = {}
    priced = health.priced_connections(r.model for r in plan.attempts)
    started = _clock()
    if not plan.auto and queue:
        _refuse_if_held(queue[0])

    while queue:
        resolved = _next(queue, failures)
        # With somewhere else to go, going there beats waiting to ask this one again.
        retries = 0 if (plan.auto and queue) else _RETRIES

        spoke = False
        announced = False

        def announce() -> Moved | None:
            nonlocal announced
            if failures and not announced:
                announced = True
                return Moved(to_model_id=resolved.model.model_id, from_model_id=failures[0][0].model.model_id,
                             reason=f"Auto moved on from {_skipped(failures)}.")
            return None

        try:
            finished: Finished | None = None
            answered_ms: int | None = None
            for tries in range(retries + 1):
                began = _clock()
                said_at: float | None = None
                try:
                    finished = None
                    for event in _events(resolved, request):
                        if isinstance(event, TextDelta):
                            if said_at is None:
                                said_at = _clock()
                            moved = announce()
                            if moved:
                                yield moved
                            spoke = True
                            yield event
                        elif isinstance(event, Finished):
                            finished = event
                    if finished is None:
                        raise ProviderError("The reply stopped part-way.", kind="reply", scope="model")
                    if not finished.tool_calls and not (finished.text or "").strip():
                        # A reply that finished cleanly and said nothing is a failure, not an
                        # answer: it is what made a turn end in an empty bubble, and what stopped
                        # Auto moving on. Here rather than in any provider module because it is
                        # true of every format — and being a ProviderError is what puts it through
                        # the same reporting, pruning and failover as a refusal or a 500.
                        raise ProviderError(
                            "The reply finished with nothing said and no tool used.", kind="reply", scope="model")
                    # How long it took to START answering (or, for a reply that was only a
                    # tool call, to finish) — what Auto's speed preference is built from.
                    answered_ms = int(((said_at if said_at is not None else _clock()) - began) * 1000)
                    break
                except ProviderError as err:
                    pause = None if (spoke or tries >= retries) else _pause_before_again(err, tries)
                    if pause is None:
                        raise
                    logger.info("model %s on %s failed (%s, %s scope); asking it again in %.1fs",
                                resolved.model.model_id, resolved.connection.label, err.kind, err.scope, pause)
                    _sleep(pause)
        except ProviderError as err:
            logger.info("model %s on %s failed: kind=%s scope=%s status=%s retry_after=%s",
                        resolved.model.model_id, resolved.connection.label, err.kind, err.scope, err.status,
                        err.retry_after_s)
            if err.scope == "request":
                # Wrong wherever it goes: said now, in the provider's words — not taken
                # to another model, and not held against this one.
                raise runtime.failure(resolved, err, named=named and not plan.auto) from err
            runtime.record(resolved, err)
            if not plan.auto:
                raise runtime.failure(resolved, err, named=named) from err
            if spoke:
                raise runtime.failure(resolved, err) from err
            failures.append((resolved, err))
            _prune(queue, resolved, err, strikes, started, priced)
            if not queue:
                raise runtime.all_failed(failures) from err
            continue

        logger.info("model %s on %s answered (began in %s ms)", resolved.model.model_id,
                    resolved.connection.label, answered_ms)
        runtime.record(resolved, None, ms=answered_ms)
        moved = announce()
        if moved:
            yield moved
        assert finished is not None
        return Result(resolved=resolved, finished=finished)

    # Only reachable with an empty plan, which `selection.plan` never returns.
    raise NoModelAvailable("No model is available.", detail={"reason": "none"})
