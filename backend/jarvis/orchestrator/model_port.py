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
    #: What the provider itself said this step consumed, as
    #: `{"unitsIn", "unitsOut", "cachedIn"}` — any key absent rather than zero
    #: when the provider did not report it. Part of the port because it is a
    #: fact about the step: every SDK already returns it and every adapter
    #: previously threw it away, which is why spend could only be guessed at.
    #: A key with no number is never filled in with one.
    usage: dict[str, Any] | None = None


@dataclass(frozen=True)
class ModelSwitched:
    """A candidate failed and the next one is taking over.

    Emitted by the gateway, not by an adapter. The orchestrator passes it
    through so a switch is never silent: a reply that changes course with no
    explanation is worse than the failure, and a turn that quietly took three
    attempts looks identical to one that took none.
    """

    to_model: str
    reason: str
    from_model: str | None = None


#: What a model client yields, in order: any number of chunks (possibly
#: interrupted by a switch), then exactly one completed step.
ModelEvent = TextChunk | StepComplete | ModelSwitched


class ModelUnavailable(RuntimeError):
    """No model could serve this turn.

    Part of the PORT rather than the gateway because it is not an internal
    error — it is a state the user has to be told accurately ("everything is
    rate-limited until about 20 past"), and flattening it into a generic
    failure is what makes an assistant feel broken when it is merely waiting.
    The orchestrator may not import the gateway, so the shared vocabulary has
    to live at the seam they already share.
    """

    def __init__(self, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.detail = detail or {}


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
        need: dict[str, bool] | None = None,
    ) -> Iterator[ModelEvent]:
        """`need` is what this turn REQUIRES — vision, video, audio, web search.

        Part of the port because it is the orchestrator's own knowledge: it is
        the side that knows an image was attached. A model that cannot see one
        must be excluded BEFORE it is called, not discovered to be blind by
        being handed bytes it cannot read.
        """
        ...
