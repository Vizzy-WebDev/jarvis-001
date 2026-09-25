"""Running one model call, and — only under Auto — trying the next model if it fails.

Both callers (the turn loop's client and the one-shot `ask`) run a `selection.Plan`
through `run`, so the rules are in one place:

* **A named model is a plan of one, and there is no second.** If it fails, the call
  fails, in the provider's own words. The one thing done for it is to try the SAME
  model again — a couple of times, a moment apart — when the provider answered
  "busy" (a 5xx), because that is the model being briefly unavailable, not a
  reason to use a different one.
* **Auto walks its list**, best first, and moves on ONLY when a model has actually
  failed: the provider refused or errored, the connection couldn't be reached or
  died, or the reply ended before a word was said — including one that finished
  cleanly having said nothing at all. It never moves on because a model is slow. A
  request the provider has accepted is left to finish however long it thinks (the
  wire layer has only a huge inactivity ceiling and TCP keep-alive for a connection
  that has truly died), and once a word has gone out no other model can take over
  the sentence, so a failure after that is reported as it is.
* **Nothing is silent.** A move is announced (`Moved`), naming what failed and why,
  and if every model fails they are all named.
* **The number of attempts is bounded by evidence, not by a count.** Two failed models
  on one connection and it is left alone for the rest of the step; a failure that is
  about the whole connection (key refused, host unreachable) leaves it at once; "the
  account needs credit" leaves its paid models but not its free ones. After a failure,
  once `auto.FIND_BUDGET_S` has passed, no model that has never worked here is
  started — that gates STARTING attempts only and never touches one in progress.
* **Going elsewhere beats asking again**: with another model waiting, Auto does not
  retry a busy one. It tries a different connection before another model on the one
  that just failed.
* Every real call's outcome is recorded — and how quickly it began to answer — for
  Auto to steer by next time.

This module imports no orchestrator and nothing that does, so a tool asking a
question through `ai.ask` can never be led into the turn loop by way of it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Generator

from ..ai import NoModelAvailable
from . import auto, providers, runtime
from .errors import ProviderError
from .selection import Plan, Resolved
from .types import Finished, TextDelta

logger = logging.getLogger(__name__)

#: A provider that answers "busy" is asked again, a moment apart, before giving up on it —
#: but only when there is nowhere else to go.
_BUSY_STATUSES = {500, 502, 503, 504, 529}
_BUSY_RETRIES = 2
_BUSY_WAITS_S = (1.0, 3.0)

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


def _events(resolved: Resolved, *, messages: list[dict[str, Any]], system: str,
            tools: list[dict[str, Any]]) -> Generator[Any, None, None]:
    try:
        provider = providers.for_format(resolved.connection.format)
        yield from provider.stream(
            resolved.target, model_id=resolved.model.model_id, messages=messages, system=system,
            tools=tools, effort=resolved.effort, facts=resolved.model.facts)
    except ProviderError:
        raise
    except _MALFORMED as err:
        logger.exception("a provider sent a reply that could not be read")
        raise ProviderError(f"{resolved.connection.label} sent part of its reply in a form Jarvis "
                            "couldn't read.", kind="reply") from err


def _busy(err: ProviderError) -> bool:
    return err.kind == "server" and err.status in _BUSY_STATUSES


def _next(queue: list[Resolved], failures: list[tuple[Resolved, ProviderError]]) -> Resolved:
    """The next to try: a different connection than any that has just failed, if there is one."""
    failed = {r.connection.id for r, _ in failures}
    for i, candidate in enumerate(queue):
        if candidate.connection.id not in failed:
            return queue.pop(i)
    return queue.pop(0)


def _prune(queue: list[Resolved], failed: Resolved, err: ProviderError, strikes: dict[str, int],
           started: float) -> None:
    """After a failure, drop from the rest of this step whatever the failure makes pointless."""
    connection_id = failed.connection.id
    strikes[connection_id] = strikes.get(connection_id, 0) + 1
    if err.kind in auto.CONNECTION_WIDE or strikes[connection_id] >= auto.STRIKES_PER_CONNECTION:
        queue[:] = [r for r in queue if r.connection.id != connection_id]
    elif err.kind == "billing":  # the account needs credit: paid models would say the same
        queue[:] = [r for r in queue if not (r.connection.id == connection_id
                                             and (r.model.facts or {}).get("free") is False)]
    if _clock() - started > auto.FIND_BUDGET_S:  # stop STARTING unknowns; never cut one in progress
        queue[:] = [r for r in queue if r.proven]


def _skipped(failures: list[tuple[Resolved, ProviderError]]) -> str:
    shown = "; ".join(f"{runtime.name_of(r)} ({runtime.brief(e, 110)})" for r, e in failures[:3])
    more = len(failures) - 3
    return shown + (f"; and {more} more" if more > 0 else "")


def run(plan: Plan, *, messages: list[dict[str, Any]], system: str, tools: list[dict[str, Any]],
        named: bool = True) -> Generator[TextDelta | Moved, None, Result]:
    """Yield what the model says as it says it; return which model answered and how.

    `named` is True when the person picked the model themselves (not a pin from a
    scheduled task), which is when a failure says Jarvis stayed on it on purpose.
    Raises `NoModelAvailable` in words when nothing could answer."""
    queue = list(plan.attempts)
    failures: list[tuple[Resolved, ProviderError]] = []
    strikes: dict[str, int] = {}
    started = _clock()

    while queue:
        resolved = _next(queue, failures)
        # With somewhere else to go, going there beats waiting to ask this one again.
        retries = 0 if (plan.auto and queue) else _BUSY_RETRIES

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
                    for event in _events(resolved, messages=messages, system=system, tools=tools):
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
                        raise ProviderError("The reply stopped part-way.", kind="reply")
                    if not finished.tool_calls and not (finished.text or "").strip():
                        # A reply that finished cleanly and said nothing is a failure, not an
                        # answer: it is what made a turn end in an empty bubble, and what stopped
                        # Auto moving on. Here rather than in any provider module because it is
                        # true of every format — and being a ProviderError is what puts it through
                        # the same reporting, pruning and failover as a refusal or a 500.
                        raise ProviderError(
                            "The reply finished with nothing said and no tool used.", kind="reply")
                    # How long it took to START answering (or, for a reply that was only a
                    # tool call, to finish) — what Auto's speed preference is built from.
                    answered_ms = int(((said_at if said_at is not None else _clock()) - began) * 1000)
                    break
                except ProviderError as err:
                    if spoke or tries >= retries or not _busy(err):
                        raise
                    _sleep(_BUSY_WAITS_S[min(tries, len(_BUSY_WAITS_S) - 1)])
        except ProviderError as err:
            runtime.record(resolved, err)
            if not plan.auto:
                raise runtime.failure(resolved, err, named=named) from err
            if spoke:
                raise runtime.failure(resolved, err) from err
            failures.append((resolved, err))
            _prune(queue, resolved, err, strikes, started)
            if not queue:
                raise runtime.all_failed(failures) from err
            continue

        runtime.record(resolved, None, ms=answered_ms)
        moved = announce()
        if moved:
            yield moved
        assert finished is not None
        return Result(resolved=resolved, finished=finished)

    # Only reachable with an empty plan, which `selection.plan` never returns.
    raise NoModelAvailable("No model is available.", detail={"reason": "none"})
