"""The turn loop's model client: the orchestrator's port, backed by the selected model.

The one module here that imports the orchestrator, and so the one that must never
be imported from anywhere a tool can reach. `assembly.py`, the composition root,
is what builds it.

A model the person NAMED is the only model that runs: there is no fallback chain
and no quiet substitution, and if it can't answer the turn fails and says why. Only
when the person has chosen Auto can more than one model be tried for a step, and
then `attempt.py` says every move out loud (`ModelSwitched`). The other time a
`ModelSwitched` is emitted is when a provider itself reports that a different model
answered than the one asked for — a fact the person is owed.
"""

from __future__ import annotations

from typing import Any, Iterator

from ..orchestrator.model_port import ModelEvent, ModelSwitched, StepComplete, TextChunk, ToolCall
from ..orchestrator.model_port import Usage as PortUsage
from . import attempt, runtime, selection
from .providers import _wire as wire
from .types import TextDelta


def _has_pictures(messages: list[dict[str, Any]]) -> bool:
    return any(kind == "image" for m in messages for kind, _, _ in wire.media_of(m))


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
        model runs it: the person's one choice — a named model, or Auto — covers
        every kind of turn."""
        plan = selection.plan(model_id, needs_images=_has_pictures(messages))
        selection.check_needs(need)

        run = attempt.run(plan, messages=messages, system=system, tools=tools, named=model_id is None)
        while True:
            try:
                event = next(run)
            except StopIteration as done:
                result = done.value
                break
            if isinstance(event, TextDelta):
                yield TextChunk(event.text)
            elif isinstance(event, attempt.Moved):
                yield ModelSwitched(to_model_id=event.to_model_id, from_model_id=event.from_model_id,
                                    reason=event.reason)
        resolved, finished = result.resolved, result.finished

        if not runtime.same_model(resolved.model.model_id, finished.model_id):
            yield ModelSwitched(
                to_model_id=finished.model_id or "",
                from_model_id=resolved.model.model_id,
                reason="the provider reported that a different model answered than the one asked for",
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
