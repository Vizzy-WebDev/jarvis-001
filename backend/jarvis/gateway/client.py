"""The gateway: pick a model, call it, and be honest when none of them work.

This is the `ModelClient` the orchestrator talks to. It owns exactly four
things the Node implementation spreads across three separate loops that disagree:

1. **Building the candidate list** — one function, `routing.build_candidates`,
   used by every caller rather than re-derived per call site.
2. **Deciding how hard to think** — the version's own scheme says what it can
   take, and `effort.plan` resolves that against a request (none, currently —
   there is no per-role default any more). An adapter is handed the answer; it
   never chooses and never clamps.
3. **Marking a failure** — every failure records the deployment's availability,
   so a model that just 401'd is not offered again on the next turn. The
   control loop in the Node app never did this, so it re-tried dead models
   forever.
4. **Saying what went wrong** — when nothing can serve the turn, the error names
   the real reasons and the soonest retry, rather than a generic apology.

**Every switch is announced**, with a `ModelSwitched` event carrying which model
gave up and why. A silent swap is cheap to implement and dishonest in both
directions: when text has already reached the user the reply visibly changes
course with no explanation, and when it has not, a turn that took three attempts
looks identical to one that took none.

**A clamp is announced too**, on the call-started event. Asking for MAX and
getting HIGH because this version's ladder stops there is the normal case
rather than an error, but a clamp nobody can see is indistinguishable from the
setting being ignored — which is how a control teaches people it does not work.
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
from ..redact import redact_text
from . import availability, deployments, effort as effort_store, latency
from .error_kind import availability_state_for, classify_error
from .jsonish import extract_json
from .routing import Role, Task, build_candidates, explain_exclusions, role_from

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


def _effort_for(entry: dict[str, Any]):
    """What to ask this deployment for, resolved against what it can take.

    No caller currently requests a specific level (there used to be a
    persisted per-role default; it was removed as an application-level
    concern), so this always resolves to the version's own scheme default —
    `plan` still does the work of answering None where there is nothing to
    send at all.
    """
    version = deployments.version_of(entry)
    if version is None:
        return None
    return effort_store.plan(
        None, version.effort,
        provider=deployments.provider_of(entry), model=str(entry.get("model") or ""))


def _effort_facts(plan: Any) -> dict[str, Any]:
    """The part of a call-started event that describes the reasoning request."""
    if plan is None:
        return {}
    facts: dict[str, Any] = {"effort": plan.level.name}
    if plan.clamped:
        facts["effortRequested"] = plan.requested.name
        facts["effortClamped"] = True
    return facts


class Gateway:
    """Constructed with no routing settings of its own, on purpose.

    `balance` used to be read once here, when the orchestrator singleton was
    built, so changing the Fast/Balanced/Quality dial did nothing until the
    process restarted. It is read per turn now, inside the ranking function, by
    the same argument that keeps a deployment's address out of its stored row:
    a copy taken at construction is a copy that can be wrong.
    """

    def __init__(self, *, event_bus: EventBus | None = None) -> None:
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
        role: Any = Role.CONVERSATION,
        need: dict[str, bool] | None = None,
    ) -> Iterator[ModelEvent]:
        task = task or Task(text=_last_user_text(messages), role=role_from(role),
                            need=dict(need or {}))
        candidates = build_candidates(task, model_id=model_id)
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

            provider = deployments.provider_of(entry)
            plan = _effort_for(entry)
            if plan is not None and plan.clamped:
                logger.info("%s takes at most %s; asked for %s", entry["id"],
                            plan.level.name, plan.requested.name)

            self._bus.publish(EventType.MODEL_CALL_STARTED, {
                "sessionId": session_id, "modelId": entry["id"], "model": entry.get("model"),
                "provider": provider, "role": task.role.value, **_effort_facts(plan)})

            if previous is not None:
                yield ModelSwitched(to_model=entry["id"], from_model=previous,
                                    reason=errors[-1][1] if errors else "the previous model failed")

            try:
                # Time to the FIRST thing the model said, not to the whole
                # answer: total duration mostly measures how long the reply was,
                # so ranking on it would learn the shape of recent questions
                # rather than anything about the model. The clock is kept by
                # `call_with_effort` because that is the only layer that knows
                # whether a first attempt was refused and retried.
                for event in effort_store.call_with_effort(
                        adapter, entry, messages, system=system, tools=tools,
                        effort=plan, provider=provider, model=entry.get("model"),
                        on_first_token=lambda ms, _id=entry["id"]: latency.record(_id, ms)):
                    if isinstance(event, TextChunk):
                        produced_text = True
                    yield event
                    if isinstance(event, StepComplete):
                        availability.record(entry["id"], "working")
                        # `usage` is what the provider itself reported, and is
                        # absent when it reported nothing. The cost observer keys
                        # on its presence: the orchestrator publishes this same
                        # event type per step WITHOUT usage, and recording both
                        # would count every turn twice.
                        self._bus.publish(EventType.MODEL_CALL_COMPLETED, {
                            "sessionId": session_id, "modelId": entry["id"],
                            "provider": provider,
                            "model": entry.get("model"),
                            "role": task.role.value,
                            "background": task.background,
                            "toolCalls": len(event.tool_calls),
                            **({"usage": event.usage} if event.usage else {})})
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

        raise NoModelAvailable(
            _all_failed_message(errors, produced_text),
            {"tried": [{"modelId": mid, "error": text} for mid, text in errors],
             **explain_exclusions(task)},
        )


def _detail_of(adapter: Any, err: BaseException) -> str:
    """The one source of a failure's text — so it is the one place to clean it.

    Everything downstream reads this: the bus event, the log line, the tried-and-
    failed list, and the message the user finally sees. A provider that quotes
    the key back in its error would otherwise put it in all four.
    """
    try:
        text = adapter.friendly_error(err) if adapter else str(err)
    except Exception:  # noqa: BLE001 — a friendly-error helper must never mask the real one
        text = str(err)
    return str(redact_text(text) or "")


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
        "retired": "no longer offered by the provider",
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
    model_id: str | None = None,
    media: list[dict[str, Any]] | None = None,
    only: bool = False,
    event_bus: EventBus | None = None,
) -> Answer:
    """A single question with no tools and no transcript — the third way to drive
    a model, alongside a turn and the control loop.

    It walks the SAME candidate list as everything else, which is the point: in
    the Node app this was a separate implementation that never marked a failing
    model unhealthy, so one-off calls kept re-trying models the chat loop had
    already benched.

    **A model that returns unparseable JSON is not marked unhealthy.** It is
    working, it is just not following a format instruction — a distinction worth
    keeping, because benching a healthy model for that would gradually empty the
    roster on exactly the weak models most likely to do it. The next candidate is
    tried instead.

    `media` rides inside the message, exactly as it does on a chat turn. `only`
    disables fallback, and is REQUIRED whenever that media was uploaded against
    one model's API key: the next candidate would be handed a URI it has no
    right to read, and would fail in a way that looks like the file being bad.
    """
    ebus = event_bus or default_bus
    task = task or Task(text=prompt, needs_tools=False, role=Role.UTILITY)
    candidates = build_candidates(task, model_id=model_id)
    if not candidates:
        raise NoModelAvailable(_nothing_available_message(task), explain_exclusions(task))

    message: dict[str, Any] = {"role": "user", "text": prompt}
    if media:
        message["media"] = media
    messages = [message]
    tried: list[str] = []
    errors: list[tuple[str, str]] = []

    attempts = candidates[:1] if only else candidates[:MAX_ATTEMPTS]
    for entry in attempts:
        try:
            adapter = get_adapter(entry.get("adapter"))
        except KeyError as err:
            errors.append((entry["id"], str(err)))
            continue

        provider = deployments.provider_of(entry)
        plan = _effort_for(entry)
        tried.append(entry["id"])
        text = ""
        usage: dict[str, Any] | None = None
        try:
            for event in effort_store.call_with_effort(
                    adapter, entry, messages, system=system, tools=[],
                    effort=plan, provider=provider, model=entry.get("model"),
                    on_first_token=lambda ms, _id=entry["id"]: latency.record(_id, ms)):
                if isinstance(event, TextChunk):
                    text += event.text
                elif isinstance(event, StepComplete):
                    usage = event.usage or usage
                    if event.text:
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
        # A one-off call is real spend too — the background checks, the memory
        # extraction and the verification pass all run through here, and a
        # subsystem that only counted chat turns would under-report exactly the
        # usage the owner has least visibility into.
        ebus.publish(EventType.MODEL_CALL_COMPLETED, {
            "modelId": entry["id"],
            "provider": provider,
            "model": entry.get("model"), "role": task.role.value,
            "background": task.background, "toolCalls": 0,
            **({"usage": usage} if usage else {})})
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
