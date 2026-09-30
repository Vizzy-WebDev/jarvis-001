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

from typing import Any, Iterator

from ..ai import NoModelAvailable
from ..orchestrator.model_port import ModelEvent, ModelSwitched, StepComplete, TextChunk, ToolCall
from ..orchestrator.model_port import Usage as PortUsage
from ..prompt_format import Instructions
from . import boundary, config, settings
from . import stream as layer_stream
from .types import Done, ErrorEvent, Hints, Request, Requirements, Section, TextDelta, Tool

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


class JarvisModelClient:
    def stream(self, *, messages: list[dict[str, Any]], system: Any, tools: list[dict[str, Any]],
               session_id: str, model_id: str | None = None, role: str | None = None,
               need: dict[str, bool] | None = None) -> Iterator[ModelEvent]:
        role = role or "conversation"
        pin = pin_for(model_id)
        sections, stable = instructions_of(system)
        request = Request(
            task_class=TASK_CLASS.get(role, role), data_class=DATA_CLASS.get(role, "personal"),
            items=boundary.items_from_conversation(messages), instructions=sections,
            tools=tuple(Tool(t["name"], t.get("description", ""),
                             t.get("parameters") or {"type": "object", "properties": {}}) for t in tools),
            requirements=Requirements(capabilities=frozenset({"image_in"} if (need or {}).get("vision") else ()),
                                      pin=pin),
            hints=Hints(stable_prefix_until=stable,
                        reasoning_effort=settings.effort() if pin == settings.SELECTED else None),
            affinity_key=session_id)

        response = None
        for event in layer_stream(request):
            if isinstance(event, TextDelta):
                yield TextChunk(event.text)
            elif isinstance(event, ErrorEvent):
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
