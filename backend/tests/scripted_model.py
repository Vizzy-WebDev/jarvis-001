"""A scripted stand-in for whatever answers a turn, plugged in at the model port.

There is no AI model system, so a test that needs "a model that says X" or "a model
that asks for tool Y" drives the real orchestrator with this client instead. It
speaks the port's own vocabulary (`orchestrator/model_port.py`), so everything
downstream of the port — the permission gate, the executor, the transcript, the
recorders — is the real thing.

    model = ScriptedModel()
    model.calls_tool("tidy_up", {}, call_id="c1")
    model.says("All done.")
    install(assembly, model)          # the orchestrator now uses it

Each call to `stream()` consumes exactly one scripted step, in order; an empty
script answers with a plain acknowledgement so an unscripted extra step is
visible rather than a hang.
"""

from __future__ import annotations

from typing import Any, Iterator

from jarvis.events.bus import bus
from jarvis.orchestrator import Orchestrator
from jarvis.orchestrator.model_port import StepComplete, TextChunk, ToolCall


class ScriptedModel:
    def __init__(self) -> None:
        self._steps: list[tuple[str, Any]] = []
        self.requests: list[dict[str, Any]] = []

    def says(self, text: str) -> None:
        self._steps.append(("say", text))

    def calls_tool(self, name: str, args: dict[str, Any] | None = None, *,
                   call_id: str = "call_1") -> None:
        self._steps.append(("call", ToolCall(id=call_id, name=name, args=dict(args or {}))))

    def stream(self, *, messages: list[dict[str, Any]], system: str, tools: list[dict[str, Any]],
               session_id: str, model_id: str | None = None, role: str | None = None,
               need: dict[str, bool] | None = None) -> Iterator[Any]:
        self.requests.append({"messages": messages, "system": system, "tools": tools,
                              "sessionId": session_id, "role": role, "need": need})
        kind, value = self._steps.pop(0) if self._steps else ("say", "OK.")
        if kind == "call":
            yield StepComplete(tool_calls=(value,), finish_reason="tool_calls",
                               model_id="scripted")
            return
        yield TextChunk(value)
        yield StepComplete(text=value, model_id="scripted")


def install(assembly: Any, model: ScriptedModel) -> ScriptedModel:
    """Make the process-wide orchestrator use `model`. `assembly.reset_for_tests()`
    undoes it."""
    assembly._orchestrator = Orchestrator(model, registry=assembly.get_registry(),
                                          event_bus=bus)
    return model
