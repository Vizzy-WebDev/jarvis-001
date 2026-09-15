"""Intelligent fallback (§12): retry, then the next deployment, then the next
model, then the next provider — never blindly, and never forever.

Four things `ai/router.py`'s ranked list already gives this module for free,
because ranking runs over every configured `ai_models` ROW (one per provider
a model is reachable through): trying the next candidate in the ranked list
already IS "try another deployment of the same model" when two rows share a
`native_model_id`, and already IS "try another model" or "try another
provider" the moment the next row differs in either. There is one fallback
loop here, not three — the distinctions in §12 are about what KIND of change
happened between two attempts, not about needing separate machinery for each.

**A bounded retry of the SAME candidate happens first, and only for a kind of
failure retrying can plausibly fix.** `errors.is_retryable()` names exactly
those (rate limit, timeout, provider overload, network) — a retry that fires
for a genuinely broken model (bad credentials, an unsupported parameter, an
invalid request) would just fail the same way twice and cost the user a
second wait for nothing.

**Every attempt is recorded, in order, whether it succeeded or not** — this
is the fallback chain §12 asks to be recorded, and §30's routing trace reads
straight off it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any, Iterator

from ..events import EventType, bus as default_bus
from ..events.bus import EventBus
from . import health, profiles
from . import usage
from .errors import benches_the_model, classify, is_retryable
from .gateway import ModelNotFound, execute_on, new_request_id
from .registry import ResolvedModel
from .request import AIRequest, Completed, ErrorEvent, ModelSwitched, StreamEvent, TextDelta, Usage
from .router import explain_exclusions, rank


def publish_call_completed(model: ResolvedModel, request: AIRequest, usage_facts: Usage | None,
                           event_bus: EventBus) -> None:
    """Announce a successful call on the shared event bus, in the vocabulary
    `observers/cost.py` already reads — this is what keeps the AI Model
    System's own spend feeding the same unified spend report TTS/STT usage
    also lands in, without this package needing to know that ledger exists."""
    payload: dict[str, Any] = {
        "sessionId": request.session_id, "modelId": model.id, "model": model.native_model_id,
        "provider": model.maker, "role": request.preferences.role.value,
        "background": request.background,
    }
    if usage_facts is not None:
        facts = {}
        if usage_facts.tokens_in is not None:
            facts["unitsIn"] = usage_facts.tokens_in
        if usage_facts.tokens_out is not None:
            facts["unitsOut"] = usage_facts.tokens_out
        if usage_facts.cached_in is not None:
            facts["cachedIn"] = usage_facts.cached_in
        if facts:
            payload["usage"] = facts
    event_bus.publish(EventType.MODEL_CALL_COMPLETED, payload)


#: How many DIFFERENT candidates one request will pay a full round trip for
#: before giving up. Without a bound, a roster where most models are stale
#: turns every request into a minutes-long walk through known failures.
MAX_CANDIDATES = 4
#: How many times the SAME candidate is retried for a retryable failure
#: before moving on. Bounded tightly — this is for a genuine blip, not a
#: substitute for choosing a healthier candidate.
MAX_RETRIES_PER_CANDIDATE = 2
RETRY_DELAY_S = 0.5


class NoModelAvailable(RuntimeError):
    """Nothing could serve this request. Carries the full attempted chain and
    why every excluded model was excluded — the routing trace §30 asks for."""

    def __init__(self, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.detail = detail or {}


@dataclass
class Attempt:
    model_id: str
    ok: bool
    error_kind: str | None = None
    error_message: str | None = None
    retries: int = 0


@dataclass
class FallbackResult:
    attempts: list[Attempt] = field(default_factory=list)


def _reason_text(reason: str) -> str:
    return {
        "disabled": "turned off", "provider_disabled": "its provider is turned off",
        "needs_key": "missing an API key", "retired": "no longer offered by the provider",
        "no_tool_calling": "unable to use tools", "context_too_small": "too small for this much text",
        "rate_limited": "rate-limited", "auth_error": "rejecting its credentials",
        "unavailable": "unavailable", "degraded": "temporarily unreliable",
        "cooling_down": "recently failed",
    }.get(reason, reason.replace("_", " "))


def _nothing_available_message(request: AIRequest) -> str:
    detail = explain_exclusions(request.requirements)
    counts = detail.get("counts") or {}
    if not counts:
        return "There are no models set up yet, so I can't answer that."
    wait = detail.get("soonestRetryMs")
    reasons = ", ".join(f"{count} {_reason_text(reason)}" for reason, count in sorted(counts.items()))
    tail = f" The soonest one should be back in about {round(wait / 60000)} minutes." if wait else ""
    return f"No model can take this right now — {reasons}.{tail}"


def _all_failed_message(attempts: list[Attempt], produced_text: bool) -> str:
    if not attempts:
        return "I couldn't get an answer from any model."
    lead = "I got part of an answer and then " if produced_text else "I tried "
    first = attempts[0].error_message or "it failed"
    return f"{lead}{len(attempts)} model{'s' if len(attempts) != 1 else ''} without success. The first said: {first}"


def candidates_for(request: AIRequest) -> list[ResolvedModel]:
    """The ranked candidate list for this request — a pin (`AIRequest.model_id`,
    the Default case) takes over ordering exactly like a soft preference:
    `rank()` moves it to the front if, and only if, it is actually eligible.
    A pin that is disabled, missing a key, or retired is simply absent from
    the list rather than forced back in — that is what keeps a pin from being
    a single point of failure the moment its model has a bad afternoon, and
    it is also why the "why can't I use X" case is not silent: `X` still
    shows up in `explain_exclusions()` with a real reason.
    """
    preferences = request.preferences
    if request.model_id:
        preferences = type(preferences)(**{**preferences.__dict__, "preferred_model_id": request.model_id})
    return rank(request.requirements, preferences)


def execute(request: AIRequest, *, event_bus: EventBus | None = None) -> Iterator[StreamEvent]:
    """Run `request` to completion, trying candidates in ranked order with a
    bounded retry on each, recording health and usage, and yielding a
    `ModelSwitched` event every time the model actually taken changes — a
    switch nobody can see is worse than the failure it hid.

    Every attempt — success or failure — is logged to `ai/usage.py` under one
    `request_id`, which is what makes the routing trace in §30 real: given a
    request id, `usage.for_request()` reconstructs exactly which candidates
    were tried, in what order, and why each one that failed did.

    The very first thing this does is resolve `request.profile_id` (or the
    default profile) against `ai/profiles.py` — this is the ONE place Default
    vs. Auto (§9) actually resolves, whether a caller named a profile,
    relied on the app's own default, or configured nothing at all.
    """
    bus = event_bus or default_bus
    request = profiles.resolve_request(request)
    request_id = request.request_id or new_request_id()
    candidates = candidates_for(request)[:MAX_CANDIDATES]
    if not candidates:
        raise NoModelAvailable(_nothing_available_message(request), explain_exclusions(request.requirements))

    result = FallbackResult()
    produced_text = False
    previous_model_id: str | None = None
    tried_chain: list[str] = []
    role = request.preferences.role.value

    for model in candidates:
        retries = 0
        while True:
            attempt_events: list[StreamEvent] = []
            attempt_usage = None
            started = time.monotonic()
            ttft_ms: float | None = None
            try:
                if previous_model_id is not None and previous_model_id != model.id:
                    yield ModelSwitched(to_model_id=model.id, from_model_id=previous_model_id,
                                        reason=result.attempts[-1].error_message or "the previous model failed")
                for event in execute_on(model, request):
                    if ttft_ms is None:
                        ttft_ms = (time.monotonic() - started) * 1000
                    if isinstance(event, TextDelta):
                        produced_text = True
                    elif isinstance(event, Completed):
                        attempt_usage = event.usage
                        # An adapter answers in terms of the wire call it just
                        # made — it has no reason to know which ranked
                        # candidate that was. This loop does, so it stamps the
                        # fact on here rather than trusting each of three
                        # adapters to independently get a cross-cutting
                        # concern right.
                        event = replace(event, model_id=model.id, provider_id=model.provider.id)
                    if isinstance(event, ErrorEvent):
                        # Swallowed here — this loop decides whether to retry
                        # or fall through; a caller sees only the final
                        # outcome's own ErrorEvent, on the last attempt.
                        continue
                    attempt_events.append(event)
                yield from attempt_events
                result.attempts.append(Attempt(model_id=model.id, ok=True, retries=retries))
                health.record_success(model.id)
                publish_call_completed(model, request, attempt_usage, bus)
                usage.record(
                    request_id=request_id, model_id=model.id, provider_id=model.provider.id,
                    role=role, success=True, usage=attempt_usage,
                    latency_ms=round((time.monotonic() - started) * 1000),
                    ttft_ms=round(ttft_ms) if ttft_ms is not None else None,
                    fallback_chain=[{"modelId": m} for m in tried_chain] or None,
                    session_id=request.session_id, background=request.background,
                )
                return
            except Exception as err:  # noqa: BLE001 — every provider failure lands here
                kind = classify(err)
                message = str(err) or kind.value
                result.attempts.append(Attempt(model_id=model.id, ok=False,
                                               error_kind=kind.value, error_message=message,
                                               retries=retries))
                if benches_the_model(kind):
                    health.record_failure(model.id, kind, detail=message, technical=repr(err))
                usage.record(
                    request_id=request_id, model_id=model.id, provider_id=model.provider.id,
                    role=role, success=False, error_type=kind.value,
                    latency_ms=round((time.monotonic() - started) * 1000),
                    ttft_ms=round(ttft_ms) if ttft_ms is not None else None,
                    session_id=request.session_id, background=request.background,
                )
                tried_chain.append(model.id)
                previous_model_id = model.id

                if is_retryable(kind) and retries < MAX_RETRIES_PER_CANDIDATE:
                    retries += 1
                    time.sleep(RETRY_DELAY_S)
                    continue
                break  # move to the next candidate

    raise NoModelAvailable(
        _all_failed_message(result.attempts, produced_text),
        {"requestId": request_id,
         "tried": [{"modelId": a.model_id, "kind": a.error_kind, "error": a.error_message}
                   for a in result.attempts],
         **explain_exclusions(request.requirements)},
    )
