"""What the orchestrator needs from a model, and nothing more (§9, §26).

The orchestrator must not know which provider answered, how streaming works on
that provider's wire, or how a tool call is spelled in its JSON. It needs three
things: text as it arrives, the tool calls a step decided on, and the fact that
a step finished. That is the whole port.

Defining it here, in the consumer, rather than in the gateway is deliberate. In
the Node implementation the turn loop and the provider layer are the same
1,027-line file, which is why a second loop (`control/session.js`) had to
reimplement the whole thing to get a different perceive step, and why neither
`ai.js` nor that loop marks a model unhealthy on failure. A narrow port means
the gateway can be replaced, stubbed, or fronted by a router without the
orchestrator noticing — and it means the orchestrator is testable against a stub
model today, before any provider code exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Protocol, runtime_checkable


@dataclass(frozen=True)
class ToolCall:
    """One tool the model asked for. `id` is the model's own correlation id."""

    id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TextChunk:
    """Streamed text, as it arrives."""

    text: str


@dataclass(frozen=True)
class StepComplete:
    """One model step finished: its full text and any tool calls it decided on.

    `raw` carries the provider's own reply object when that provider needs exact
    round-tripping (Gemini's `thought_signature` must come back verbatim or the
    next call is rejected). The orchestrator never inspects it — it only stores
    it on the transcript so the adapter gets it back unchanged.
    """

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    model_id: str | None = None
    raw: dict[str, Any] | None = None


#: What a model client yields, in order: any number of chunks, then one step.
ModelEvent = TextChunk | StepComplete


@runtime_checkable
class ModelClient(Protocol):
    """A provider-agnostic single step of generation."""

    def stream(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]],
        session_id: str,
    ) -> Iterator[ModelEvent]:
        ...
