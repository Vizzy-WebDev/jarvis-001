"""A scripted model that keeps one script PER SPEAKER, for multi-agent tests.

`scripted_model.ScriptedModel` consumes one global script in call order, which
cannot describe "Jarvis asks Advertising, Advertising asks Research, Research
answers" without the test knowing exactly how the threads interleave. Here each
turn's session says who is speaking — `agent:<id>:…` is that agent, anything else
is Jarvis — and each speaker has its own queue.

    model = SessionScriptedModel()
    model.on("jarvis").calls_tool("ask_specialist", {"agent": "research", "task": "..."})
    model.on("research").says("Here is what I found.")
    model.on("jarvis").says("Research found ...")
    install(assembly, model)

Every request is kept (`requests`, with the speaker) so a test can assert on the
exact system prompt, tools and model pin each speaker was given.
"""

from __future__ import annotations

import re
import threading
from typing import Any, Iterator

from jarvis.events.bus import bus
from jarvis.orchestrator import Orchestrator
from jarvis.orchestrator.model_port import StepComplete, TextChunk, ToolCall


class _Script:
    def __init__(self) -> None:
        self.steps: list[tuple[str, Any]] = []
        #: Instead of a fixed order: a function of the messages this speaker was
        #: sent, returning ("say", text) or ("call", ToolCall). For concurrent turns,
        #: where a shared ordered script would hand one turn another's step.
        self.responder: Any = None

    def responds(self, fn: Any) -> "_Script":
        self.responder = fn
        return self

    def says(self, text: str) -> "_Script":
        self.steps.append(("say", text))
        return self

    def calls_tool(self, name: str, args: dict[str, Any] | None = None, *,
                   call_id: str | None = None) -> "_Script":
        cid = call_id or f"call_{len(self.steps) + 1}"
        self.steps.append(("call", ToolCall(id=cid, name=name, args=dict(args or {}))))
        return self


_SPECIALIST_LINE = re.compile(r"^You are (.+?), one of Jarvis's specialist agents\.")


def speaker_of(session_id: str, system: str = "") -> str:
    """Who is speaking: the agent a specialist prompt names (whatever session it
    runs on — a background job's, a direct chat's), otherwise Jarvis."""
    found = _SPECIALIST_LINE.match(system or "")
    if found:
        from jarvis.agents import store

        agent = store.find_agent(found.group(1))
        if agent is not None:
            return agent["id"]
    if session_id.startswith("agent:"):
        return session_id.split(":")[1]
    return "jarvis"


class SessionScriptedModel:
    def __init__(self) -> None:
        self._scripts: dict[str, _Script] = {}
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        #: A session id → speaker override, for a direct chat with an agent (which
        #: runs on the ordinary conversation session).
        self.speaker_for_session: dict[str, str] = {}

    def on(self, speaker: str) -> _Script:
        with self._lock:
            return self._scripts.setdefault(speaker, _Script())

    def requests_of(self, speaker: str) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["speaker"] == speaker]

    def stream(self, *, messages: list[dict[str, Any]], system: str, tools: list[dict[str, Any]],
               session_id: str, model_id: str | None = None, role: str | None = None,
               need: dict[str, bool] | None = None) -> Iterator[Any]:
        speaker = self.speaker_for_session.get(session_id) or speaker_of(session_id, system)
        with self._lock:
            self.requests.append({"speaker": speaker, "messages": list(messages), "system": system,
                                  "tools": [t["name"] for t in tools], "sessionId": session_id,
                                  "modelId": model_id, "role": role})
            script = self._scripts.setdefault(speaker, _Script())
            if script.responder is not None:
                kind, value = script.responder(messages)
            else:
                kind, value = script.steps.pop(0) if script.steps else ("say", "OK.")
        if kind == "call":
            yield StepComplete(tool_calls=(value,), finish_reason="tool_calls", model_id="scripted")
            return
        yield TextChunk(value)
        yield StepComplete(text=value, model_id="scripted")


def install(assembly: Any, model: SessionScriptedModel) -> SessionScriptedModel:
    assembly._orchestrator = Orchestrator(model, registry=assembly.get_registry(), event_bus=bus)
    return model
