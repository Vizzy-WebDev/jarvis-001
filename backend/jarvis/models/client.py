"""The turn loop's model client: the orchestrator's port, backed by the selected model.

The one module here that imports the orchestrator, and so the one that must never
be imported from anywhere a tool can reach. `assembly.py`, the composition root,
is what builds it.

It never switches models. There is no fallback chain and no quiet substitution:
if the selection cannot be run, the turn fails and says why. The one time it
emits a `ModelSwitched` is when a provider itself reports that a different model
answered than the one asked for — a fact the person is owed, not a fallback.
"""

from __future__ import annotations

from typing import Any, Iterator

from ..orchestrator.model_port import ModelEvent, ModelSwitched, StepComplete, TextChunk, ToolCall
from ..orchestrator.model_port import Usage as PortUsage
from . import providers, runtime, selection
from .errors import ProviderError
from .types import Finished, TextDelta


class JarvisModelClient:
    def stream(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]],
        session_id: str,
        model_id: str | None = None,
        role: str | None = None,
        need: dict[str, bool] | None = None,
    ) -> Iterator[ModelEvent]:
        """`role` says what kind of turn this is and changes nothing about which
        model runs it: there is one selected model, and every kind of turn uses it."""
        resolved = selection.resolve(model_id)
        selection.check_needs(need)
        provider = providers.for_format(resolved.connection.format)

        finished: Finished | None = None
        try:
            for event in provider.stream(
                resolved.target, model_id=resolved.model.model_id, messages=messages,
                system=system, tools=tools, effort=resolved.effort, facts=resolved.model.facts,
            ):
                if isinstance(event, TextDelta):
                    yield TextChunk(event.text)
                elif isinstance(event, Finished):
                    finished = event
        except ProviderError as err:
            raise runtime.failure(resolved, err) from err
        if finished is None:
            raise runtime.failure(resolved, ProviderError("The reply stopped part-way.", kind="reply"))

        if not runtime.same_model(resolved.model.model_id, finished.model_id):
            yield ModelSwitched(
                to_model_id=finished.model_id or "",
                from_model_id=resolved.model.model_id,
                reason="the provider reported that a different model answered than the one you picked",
            )

        runtime.publish_completed(
            resolved, session_id=session_id, reported=finished.model_id, usage=finished.usage,
            background=(role in runtime.BACKGROUND_ROLES),
        )
        usage = finished.usage
        yield StepComplete(
            text=finished.text,
            tool_calls=tuple(ToolCall(id=c.id, name=c.name, args=c.args) for c in finished.tool_calls),
            finish_reason=finished.finish_reason,
            usage=PortUsage(tokens_in=usage.tokens_in, tokens_out=usage.tokens_out,
                            tokens_reasoning=usage.tokens_reasoning, cached_in=usage.cached_in) if usage else None,
            # What the PROVIDER said answered — not what was asked for.
            model_id=finished.model_id or resolved.model.model_id,
            provider_id=resolved.connection.id,
            raw=finished.raw,
        )
