"""Running one model call, and — only under Auto — trying the next model if it fails.

Both callers (the turn loop's client and the one-shot `ask`) run a `selection.Plan`
through `run`, so the rules are in one place:

* **A named model is a plan of one, and there is no second.** If it fails, the call
  fails, in the provider's own words. The one thing done for it is to try the SAME
  model again — a couple of times, a moment apart — when the provider answered
  "busy" (a 5xx), because that is the model being briefly unavailable, not a
  reason to use a different one.
* **Auto walks its list**, best first, up to `auto.MAX_ATTEMPTS` models. It moves on
  only if the model failed BEFORE saying anything: once a word has gone out, another
  model can't take over the sentence, so the failure is reported as it is. It tries
  a different connection before another model on the one that just failed, and it
  gives a silent model less time to answer while others are still waiting.
* **Nothing is silent.** A move is announced (`Moved`), naming what failed and why,
  and if every model fails they are all named.
* Every real call's outcome is recorded, for Auto to steer by next time.

This module imports no orchestrator and nothing that does, so a tool asking a
question through `ai.ask` can never be led into the turn loop by way of it.
"""

from __future__ import annotations

import dataclasses
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

#: A provider that answers "busy" is asked again, a moment apart, before giving up on it.
_BUSY_STATUSES = {500, 502, 503, 504, 529}
_BUSY_RETRIES = 2
_BUSY_WAITS_S = (1.0, 3.0)

#: What a provider module can do with a reply it wasn't expecting. These become the
#: same plain "couldn't read the reply" as any other, rather than a crash.
_MALFORMED = (KeyError, TypeError, ValueError, IndexError, AttributeError)


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


def _events(resolved: Resolved, target: Any, *, messages: list[dict[str, Any]], system: str,
            tools: list[dict[str, Any]]) -> Generator[Any, None, None]:
    try:
        provider = providers.for_format(resolved.connection.format)
        yield from provider.stream(
            target, model_id=resolved.model.model_id, messages=messages, system=system,
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


def run(plan: Plan, *, messages: list[dict[str, Any]], system: str, tools: list[dict[str, Any]],
        named: bool = True) -> Generator[TextDelta | Moved, None, Result]:
    """Yield what the model says as it says it; return which model answered and how.

    `named` is True when the person picked the model themselves (not a pin from a
    scheduled task), which is when a failure says Jarvis stayed on it on purpose.
    Raises `NoModelAvailable` in words when nothing could answer."""
    queue = list(plan.attempts)
    failures: list[tuple[Resolved, ProviderError]] = []
    started = time.monotonic()

    while queue:
        resolved = _next(queue, failures)
        others_wait = plan.auto and bool(queue) and len(failures) + 1 < auto.MAX_ATTEMPTS
        target = dataclasses.replace(resolved.target, read_timeout=auto.FAST_READ_TIMEOUT_S) if others_wait \
            else resolved.target
        retries = 1 if others_wait else _BUSY_RETRIES  # with somewhere else to go, don't linger

        spoke = False
        announced = False

        def announce() -> Moved | None:
            nonlocal announced
            if failures and not announced:
                announced = True
                first = failures[0][0]
                skipped = "; ".join(f"{runtime.name_of(r)} ({runtime.brief(e, 110)})" for r, e in failures)
                return Moved(to_model_id=resolved.model.model_id, from_model_id=first.model.model_id,
                             reason=f"Auto moved on from {skipped}.")
            return None

        try:
            finished: Finished | None = None
            for tries in range(retries + 1):
                try:
                    finished = None
                    for event in _events(resolved, target, messages=messages, system=system, tools=tools):
                        if isinstance(event, TextDelta):
                            moved = announce()
                            if moved:
                                yield moved
                            spoke = True
                            yield event
                        elif isinstance(event, Finished):
                            finished = event
                    if finished is None:
                        raise ProviderError("The reply stopped part-way.", kind="reply")
                    break
                except ProviderError as err:
                    if spoke or tries >= retries or not _busy(err):
                        raise
                    time.sleep(_BUSY_WAITS_S[min(tries, len(_BUSY_WAITS_S) - 1)])
        except ProviderError as err:
            runtime.record(resolved, err)
            if not plan.auto:
                raise runtime.failure(resolved, err, named=named) from err
            if spoke:
                raise runtime.failure(resolved, err) from err
            failures.append((resolved, err))
            if err.kind == "billing":
                # The account needs credit: the paid models on this connection would say the
                # same, so they are skipped for the rest of this step. Free ones are not.
                queue[:] = [r for r in queue if not (r.connection.id == resolved.connection.id
                                                     and (r.model.facts or {}).get("free") is False)]
            if (len(failures) >= auto.MAX_ATTEMPTS or not queue
                    or time.monotonic() - started > auto.TIME_BUDGET_S):
                raise runtime.all_failed(failures) from err
            continue

        runtime.record(resolved, None)
        moved = announce()
        if moved:
            yield moved
        assert finished is not None
        return Result(resolved=resolved, finished=finished)

    # Only reachable with an empty plan, which `selection.plan` never returns.
    raise NoModelAvailable("No model is available.", detail={"reason": "none"})
