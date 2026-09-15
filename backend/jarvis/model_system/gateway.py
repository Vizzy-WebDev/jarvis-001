"""The AI Model Gateway — the one entry point for executing an `AIRequest`.

    caller -> execute(AIRequest)
                -> resolve the model (a pin today; the router in
                   `ai/router.py` decides it when nothing is pinned)
                -> resolve reasoning + generation params against what THIS
                   model actually accepts
                -> the wire-format adapter
                -> normalized StreamEvents back to the caller

This module currently drives exactly one resolved model per call — pinned by
`AIRequest.model_id`, or chosen once by the caller. `ai/router.py` (ranking)
and `ai/fallback.py` (retry / trying the next candidate) sit in FRONT of this
module, calling `execute_on()` once per candidate they choose to try; this
file does not loop over candidates itself, so the retry/fallback policy lives
in exactly one place rather than being duplicated into the thing it calls.
"""

from __future__ import annotations

import uuid
from typing import Any, Iterator

from . import errors
from .adapters import get_adapter
from .parameters import build_params
from .reasoning import plan as plan_reasoning
from .registry import ResolvedModel, get_model
from .request import (
    AIRequest, AIResponse, Completed, ErrorEvent, StreamEvent, TextDelta, ToolCallReady,
)


class ModelNotFound(RuntimeError):
    pass


def new_request_id() -> str:
    return uuid.uuid4().hex


def _friendly_error(adapter, err: BaseException) -> str:
    try:
        return adapter.friendly_error(err)
    except Exception:  # noqa: BLE001 — a friendly-error helper must never mask the real one
        return str(err) or "That model didn't answer."


def execute_on(model: ResolvedModel, request: AIRequest) -> Iterator[StreamEvent]:
    """Run `request` against exactly this model. No fallback, no ranking —
    the caller (`ai/fallback.py`, or a test) has already decided this is the
    candidate to try.

    Raises the adapter's own exception on failure, after yielding an
    `ErrorEvent` so a streaming caller sees why before the exception
    propagates. `ai/errors.classify()` is how a caller turns that exception
    into a decision (bench the model? retry? try the next candidate?).
    """
    adapter = get_adapter(model.provider.adapter)
    reasoning_request = plan_reasoning(request.reasoning, model.reasoning)
    params = build_params(request.params, model.parameters)

    try:
        yield from adapter.stream(
            model.provider, model.native_model_id, request.messages, request.system,
            request.tools, reasoning=reasoning_request, params=params,
            response_format=request.response_format,
        )
    except Exception as err:  # noqa: BLE001 — every provider failure lands here
        kind = errors.classify(err)
        yield ErrorEvent(kind=kind.value, message=_friendly_error(adapter, err))
        raise


def execute(request: AIRequest) -> Iterator[StreamEvent]:
    """Resolve `request.model_id` and run it. Raises `ModelNotFound` for an
    unknown pin. A request with no pin at all is Auto — `ai/router.py`
    resolves that case; this function is the pinned/Default path plus the
    execution primitive the router's fallback loop calls into.
    """
    if not request.model_id:
        raise ModelNotFound("No model was specified and nothing has chosen one yet.")
    model = get_model(request.model_id)
    if model is None:
        raise ModelNotFound(f"Unknown model: {request.model_id}")
    yield from execute_on(model, request)


def run(request: AIRequest) -> AIResponse:
    """The non-streaming convenience shape — the same facts a stream ends
    with, collected into one value."""
    text_parts: list[str] = []
    completed: Completed | None = None
    for event in execute(request):
        if isinstance(event, TextDelta):
            text_parts.append(event.text)
        elif isinstance(event, Completed):
            completed = event

    if completed is None:
        # execute() always ends in a Completed on success, or raises — this
        # is defensive, not a real path.
        return AIResponse(text="".join(text_parts), request_id=request.request_id)

    return AIResponse(
        text=completed.text or "".join(text_parts),
        tool_calls=completed.tool_calls,
        finish_reason=completed.finish_reason,
        usage=completed.usage,
        model_id=request.model_id,
        provider_id=completed.provider_id,
        request_id=request.request_id,
        structured_data=completed.structured_data,
        raw=completed.raw,
    )


class Gateway:
    """The seam the orchestrator (`orchestrator/pipeline.py`) and the desktop
    control loop (`control/session.py`) both hold a `ModelClient` through —
    see `orchestrator/model_port.py`, which now sources its whole vocabulary
    straight from `model_system.request`.

    Both callers speak the transcript store's own plain-dict shape
    (`{"role", "text", "media", "toolCalls", "toolResults", "raw",
    "interrupted"}`) and the capability registry's own tool-declaration dicts
    — neither is part of this rebuild, so this class is the one place that
    vocabulary becomes an `AIRequest`. Everything past that point — routing,
    retry, fallback, health, usage — is `ai/fallback.execute()`, unchanged
    from what any other caller of this package gets.

    Constructed with no routing settings of its own, on purpose — the same
    reasoning the previous build's own `Gateway` documented: a `balance`
    baked in at construction is a setting that silently stops working until
    the process restarts. `ai/router.py` reads it fresh, per call.
    """

    def __init__(self, *, event_bus: Any = None) -> None:
        self._bus = event_bus

    def stream(
        self,
        *,
        messages: list[dict],
        system: str,
        tools: list[dict],
        session_id: str,
        model_id: str | None = None,
        role: Any = None,
        need: dict[str, bool] | None = None,
    ) -> Iterator[StreamEvent]:
        from . import fallback
        from .legacy_bridge import messages_from_dicts, tools_from_declarations
        from .request import Preferences, Requirements, role_from

        request = AIRequest(
            messages=messages_from_dicts(messages), system=system,
            tools=tools_from_declarations(tools),
            # Every ordinary turn offers the capability registry to the
            # model, so a model confirmed unable to call tools at all is
            # unusable for chat regardless of what this one turn happens to
            # be asking — matching the previous build's own default.
            requirements=Requirements(needs_tools=True, capabilities=dict(need or {})),
            preferences=Preferences(role=role_from(role)),
            model_id=model_id, session_id=session_id,
        )
        yield from fallback.execute(request, event_bus=self._bus)
