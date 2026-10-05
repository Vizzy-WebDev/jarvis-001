"""The turn loop's model client: the orchestrator's port, over the model layer.

The one module here that imports the orchestrator, and so the one that must never
be imported from anywhere a tool can reach. `assembly.py` builds it.

It turns the stored conversation into a typed request — task class and data class
from the kind of turn, the session as affinity key, the person's selection as a
pin (Auto: none), their effort as a hint — streams it, and maps the result back:
text as it comes, every fallback as a `ModelSwitched`, the cost ledger event, and
one `StepComplete` carrying what the server said answered.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator

from ..ai import NoModelAvailable
from ..orchestrator.model_port import ModelEvent, ModelSwitched, StepComplete, TextChunk, ToolCall
from ..orchestrator.model_port import Usage as PortUsage
from ..prompt_format import Instructions
from . import boundary, config, settings
from . import stream as layer_stream
from .types import Done, ErrorEvent, Hints, Request, Requirements, Section, TextDelta, Tool

logger = logging.getLogger(__name__)

#: What kind of work each turn role is, and how sensitive what it carries is. Every
#: turn-loop role carries the person's conversation, memories or screen: personal.
TASK_CLASS = {"conversation": "chat", "voice": "chat", "control": "control", "background": "background",
              "utility": "utility"}
DATA_CLASS = {"conversation": "personal", "voice": "personal", "control": "personal", "background": "personal",
              "utility": "personal"}
BACKGROUND_ROLES = {"background", "utility"}


def instructions_of(system: Any) -> tuple[tuple[Section, ...], str | None]:
    if isinstance(system, Instructions):
        return tuple(Section(label, text) for label, text in system.sections), system.stable_prefix_until
    return ((Section("instructions", str(system)),) if system else ()), None


def pin_for(model_id: str | None) -> str | None:
    if model_id:
        # A specialist's or scheduled task's pin is an alias of the same name — made the
        # first time it is used, from the one endpoint serving that model id.
        try:
            return settings.ensure_pin(model_id)
        except LookupError as err:
            raise NoModelAvailable(f"{err} Choose it on the Model Settings screen, or clear the pin.",
                                   detail={"reason": "missing_model"}) from err
    try:
        return None if settings.is_auto() else settings.SELECTED
    except config.ConfigError:
        return None


def _request(*, messages: list[dict[str, Any]], system: Any, tools: list[dict[str, Any]], session_id: str,
             pin: str | None, role: str, need: dict[str, bool] | None) -> Request:
    sections, stable = instructions_of(system)
    return Request(
        task_class=TASK_CLASS.get(role, role), data_class=DATA_CLASS.get(role, "personal"),
        items=boundary.items_from_conversation(messages), instructions=sections,
        tools=tuple(Tool(t["name"], t.get("description", ""),
                         t.get("parameters") or {"type": "object", "properties": {}}) for t in tools),
        requirements=Requirements(capabilities=frozenset({"image_in"} if (need or {}).get("vision") else ()),
                                  pin=pin),
        hints=Hints(stable_prefix_until=stable,
                    reasoning_effort=settings.effort() if pin == settings.SELECTED else None),
        affinity_key=session_id)


def _learn_from_refusal(error: Any, request: Request) -> None:
    """A model refused this request as too long: what it refused is a real upper bound on its
    window. Half the refused size is recorded — a search step down from the model's own answer,
    never a size chosen in advance — and the turn loop re-assembles to fit it."""
    if getattr(error, "type", None) != "context_too_long" or not getattr(error, "endpoint_id", None):
        return
    try:
        from . import state
        from .resolve import estimate_tokens

        refused = estimate_tokens(request, config.current().settings.image_tokens)
        state.record_learned_context(error.endpoint_id, refused // 2)
    except Exception:  # noqa: BLE001 — learning must never mask the refusal itself
        logger.exception("could not record the context window %s refused", error.endpoint_id)


class JarvisModelClient:
    def context_capacity(self, *, session_id: str, model_id: str | None = None, role: str | None = None,
                         need: dict[str, bool] | None = None, tools: list[dict[str, Any]] | None = None,
                         ) -> tuple[int | None, int | None] | None:
        """How much the model that would answer this turn can take: (context window, max output),
        each None when the model has never said. Routed exactly as a real call is — the person's
        pinned model, or what Auto would pick — with no model called. None when nothing would."""
        from .capabilities import limit
        from .engine import route

        try:
            request = _request(messages=[], system="", tools=list(tools or []), session_id=session_id,
                               pin=pin_for(model_id), role=role or "conversation", need=need)
            ranked, _ = route(request)
        except Exception:  # noqa: BLE001 — not knowing is an answer; it must never fail a turn
            logger.exception("could not work out the answering model's capacity")
            return None
        if not ranked:
            return None
        caps = ranked[0].endpoint.capabilities
        return limit(caps, "max_context_tokens"), limit(caps, "max_output_tokens")

    def stream(self, *, messages: list[dict[str, Any]], system: Any, tools: list[dict[str, Any]],
               session_id: str, model_id: str | None = None, role: str | None = None,
               need: dict[str, bool] | None = None) -> Iterator[ModelEvent]:
        role = role or "conversation"
        pin = pin_for(model_id)
        request = _request(messages=messages, system=system, tools=tools, session_id=session_id,
                           pin=pin, role=role, need=need)

        response = None
        for event in layer_stream(request):
            if isinstance(event, TextDelta):
                yield TextChunk(event.text)
            elif isinstance(event, ErrorEvent):
                _learn_from_refusal(event.error, request)
                raise boundary.failure(event.error, pinned_selection=pin == settings.SELECTED)
            elif isinstance(event, Done):
                response = event.response
        assert response is not None

        for moved in response.provenance.fallbacks:
            yield ModelSwitched(to_model_id=moved.to_endpoint, from_model_id=moved.from_endpoint,
                                reason=f"Moved on from {moved.from_endpoint}: {moved.reason}")
        boundary.refuse_raw_arguments(response)
        boundary.publish_completed(response, session_id=session_id, background=role in BACKGROUND_ROLES)
        usage = response.usage
        yield StepComplete(
            text=response.text,
            tool_calls=tuple(ToolCall(id=c.id, name=c.name, args=dict(c.arguments or {}))
                             for c in response.tool_calls),
            finish_reason=response.stop_reason,
            usage=PortUsage(tokens_in=usage.input, tokens_out=usage.output, tokens_reasoning=usage.reasoning,
                            cached_in=usage.cached),
            model_id=boundary.reported_model(response),
            provider_id=response.provenance.endpoint_id.split("/", 1)[0],
            raw=boundary.raw_for(response),
        )
