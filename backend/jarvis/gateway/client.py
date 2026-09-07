"""The gateway: pick a model, call it, and be honest when none of them work.

This is the `ModelClient` the orchestrator talks to. It owns exactly three
things the Node implementation spreads across three separate loops that disagree:

1. **Building the candidate list** — one function, `routing.build_candidates`,
   used by every caller rather than re-derived per call site.
2. **Marking a failure** — every failure records the model's availability, so a
   model that just 401'd is not offered again on the next turn. The control loop
   in the Node app never did this, so it re-tried dead models forever.
3. **Saying what went wrong** — when nothing can serve the turn, the error names
   the real reasons and the soonest retry, rather than a generic apology.

**Every switch is announced**, with a `ModelSwitched` event carrying which model
gave up and why. A silent swap is cheap to implement and dishonest in both
directions: when text has already reached the user the reply visibly changes
course with no explanation, and when it has not, a turn that took three attempts
looks identical to one that took none.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterator

from ..adapters import get_adapter
from ..events import EventType, bus as default_bus
from ..events.bus import EventBus
from ..orchestrator.model_port import (
    ModelEvent, ModelSwitched, ModelUnavailable, StepComplete, TextChunk,
)
from . import availability
from .error_kind import availability_state_for, classify_error
from .jsonish import extract_json
from .routing import Task, build_candidates, explain_exclusions

logger = logging.getLogger(__name__)

#: How many models one step will pay a full round trip for before giving up.
#: Without a bound, a roster where most models are stale turns every turn into a
#: minutes-long walk through known failures.
MAX_ATTEMPTS = 4


class NoModelAvailable(ModelUnavailable):
    """Nothing could serve this turn. Carries the real reasons.

    A subclass of the port's own type so the orchestrator can recognise it
    without importing this module — see model_port.ModelUnavailable.
    """


class Gateway:
    def __init__(self, *, balance: str = "balanced", event_bus: EventBus | None = None) -> None:
        self.balance = balance
        self._bus = event_bus or default_bus

    def stream(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]],
        session_id: str,
        task: Task | None = None,
        model_id: str | None = None,
        manual_model_id: str | None = None,
    ) -> Iterator[ModelEvent]:
        task = task or Task(text=_last_user_text(messages))
        candidates = build_candidates(task, balance=self.balance,
                                      model_id=model_id, manual_model_id=manual_model_id)
        if not candidates:
            raise NoModelAvailable(_nothing_available_message(task), explain_exclusions(task))

        produced_text = False
        previous: str | None = None
        errors: list[tuple[str, str]] = []

        for entry in candidates[:MAX_ATTEMPTS]:
            adapter = None
            try:
                adapter = get_adapter(entry.get("adapter"))
            except KeyError as err:
                errors.append((entry["id"], str(err)))
                continue

            self._bus.publish(EventType.MODEL_CALL_STARTED, {
                "sessionId": session_id, "modelId": entry["id"], "model": entry.get("model"),
                "provider": entry.get("provider") or entry.get("adapter")})

            if previous is not None:
                yield ModelSwitched(to_model=entry["id"], from_model=previous,
                                    reason=errors[-1][1] if errors else "the previous model failed")

            step_text = False
            try:
                for event in adapter.stream(entry, messages, system=system, tools=tools):
                    if isinstance(event, TextChunk):
                        step_text = produced_text = True
                    yield event
                    if isinstance(event, StepComplete):
                        availability.record(entry["id"], "working")
                        self._bus.publish(EventType.MODEL_CALL_COMPLETED, {
                            "sessionId": session_id, "modelId": entry["id"],
                            "toolCalls": len(event.tool_calls)})
                        return
                # A stream that ends without completing a step is a broken
                # provider response, not a silent success.
                raise RuntimeError("the provider ended the stream without a final message")
            except Exception as err:  # noqa: BLE001 — every provider failure lands here
                kind = classify_error(err)
                detail = _detail_of(adapter, err)
                availability.record(entry["id"], availability_state_for(err), detail=detail,
                                    technical=str(err))
                self._bus.publish(EventType.MODEL_CALL_FAILED, {
                    "sessionId": session_id, "modelId": entry["id"], "kind": kind,
                    "error": detail})
                logger.warning("model %s failed (%s): %s", entry["id"], kind, detail)
                errors.append((entry["id"], detail))
                previous = entry["id"]
                # `step_text` matters only for the message at the end: a turn
                # that already showed the user text reads differently from one
                # that never started.
                del step_text

        raise NoModelAvailable(
            _all_failed_message(errors, produced_text),
            {"tried": [{"modelId": mid, "error": text} for mid, text in errors],
             **explain_exclusions(task)},
        )


def _detail_of(adapter: Any, err: BaseException) -> str:
    try:
        return adapter.friendly_error(err) if adapter else str(err)
    except Exception:  # noqa: BLE001 — a friendly-error helper must never mask the real one
        return str(err)


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user" and message.get("text"):
            return message["text"]
    return ""


def _nothing_available_message(task: Task) -> str:
    detail = explain_exclusions(task)
    counts = detail.get("counts") or {}
    if not counts:
        return "There are no models set up yet, so I can't answer that."
    wait = detail.get("soonestRetryMs")
    reasons = ", ".join(f"{count} {_reason_text(reason)}" for reason, count in sorted(counts.items()))
    tail = f" The soonest one should be back in about {round(wait / 60000)} minutes." if wait else ""
    return f"No model can take this right now — {reasons}.{tail}"


def _all_failed_message(errors: list[tuple[str, str]], produced_text: bool) -> str:
    if not errors:
        return "I couldn't get an answer from any model."
    first = errors[0][1]
    lead = "I got part of an answer and then " if produced_text else "I tried "
    return f"{lead}{len(errors)} model{'s' if len(errors) != 1 else ''} without success. The first said: {first}"


def _reason_text(reason: str) -> str:
    return {
        "disabled": "turned off",
        "needs_key": "missing an API key",
        "quota": "out of quota",
        "auth": "rejecting their key",
        "no_access": "not allowed for this key",
        "busy": "busy at the provider",
        "unreachable": "unreachable",
        "unsupported": "unable to handle this kind of request",
        "error": "recently failed",
        "no_tools": "unable to use tools",
        "context_too_small": "too small for this much text",
    }.get(reason, reason.replace("_", " "))


@dataclass
class Answer:
    """One prompt, one answer, no tools."""

    text: str
    model_id: str | None = None
    data: Any = None
    tried: list[str] = field(default_factory=list)


def ask(
    prompt: str,
    *,
    system: str = "",
    want_json: bool = False,
    task: Task | None = None,
    balance: str = "balanced",
    model_id: str | None = None,
    event_bus: EventBus | None = None,
) -> Answer:
    """A single question with no tools and no transcript — the third way to drive
    a model, alongside a turn and (later) the control loop.

    It walks the SAME candidate list as everything else, which is the point: in
    the Node app this was a separate implementation that never marked a failing
    model unhealthy, so one-off calls kept re-trying models the chat loop had
    already benched.

    **A model that returns unparseable JSON is not marked unhealthy.** It is
    working, it is just not following a format instruction — a distinction worth
    keeping, because benching a healthy model for that would gradually empty the
    roster on exactly the weak models most likely to do it. The next candidate is
    tried instead.
    """
    ebus = event_bus or default_bus
    task = task or Task(text=prompt, needs_tools=False)
    candidates = build_candidates(task, balance=balance, model_id=model_id)
    if not candidates:
        raise NoModelAvailable(_nothing_available_message(task), explain_exclusions(task))

    messages = [{"role": "user", "text": prompt}]
    tried: list[str] = []
    errors: list[tuple[str, str]] = []

    for entry in candidates[:MAX_ATTEMPTS]:
        try:
            adapter = get_adapter(entry.get("adapter"))
        except KeyError as err:
            errors.append((entry["id"], str(err)))
            continue

        tried.append(entry["id"])
        text = ""
        try:
            for event in adapter.stream(entry, messages, system=system, tools=[]):
                if isinstance(event, TextChunk):
                    text += event.text
                elif isinstance(event, StepComplete) and event.text:
                    text = event.text
        except Exception as err:  # noqa: BLE001
            detail = _detail_of(adapter, err)
            availability.record(entry["id"], availability_state_for(err), detail=detail,
                                technical=str(err))
            ebus.publish(EventType.MODEL_CALL_FAILED, {
                "modelId": entry["id"], "kind": classify_error(err), "error": detail})
            errors.append((entry["id"], detail))
            continue

        availability.record(entry["id"], "working")
        if not want_json:
            return Answer(text=text, model_id=entry["id"], tried=tried)

        data = extract_json(text)
        if data is None:
            # Deliberately no availability record: see this function's docstring.
            logger.info("model %s answered but not as JSON; trying the next", entry["id"])
            errors.append((entry["id"], "did not answer in the requested format"))
            continue
        return Answer(text=text, model_id=entry["id"], data=data, tried=tried)

    raise NoModelAvailable(_all_failed_message(errors, produced_text=False),
                           {"tried": [{"modelId": m, "error": e} for m, e in errors]})
