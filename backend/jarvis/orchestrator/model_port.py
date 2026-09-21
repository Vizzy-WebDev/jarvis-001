"""What the orchestrator needs from whatever answers a turn, and nothing more.

The turn loop must not know which provider answered, how streaming works on
that provider's wire, or how a tool call is spelled in its JSON. This port is
the whole of what it asks for: a `ModelClient` that streams `TextChunk`s (and
possibly a `ModelSwitched`), then exactly one `StepComplete`. The desktop
control loop (`control/session.py`) reuses the same port for its perceive step.

The event shapes below are the orchestrator's own vocabulary. There is no AI
model system behind this port at the moment: `NoModelClient` is what runs until
one is built, and every turn it serves ends in `ModelUnavailable`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping, Protocol, runtime_checkable

from ..ai import NO_MODEL_MESSAGE
from ..ai import NoModelAvailable as ModelUnavailable


@dataclass(frozen=True)
class ToolCall:
    """One tool a model asked to run. `id` is the model's own correlation id —
    handed back unchanged when the caller supplies the result."""

    id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Usage:
    """What a provider itself reported this call consumed. A field left `None`
    means the provider did not report it — never a zero standing in for "not
    measured"."""

    tokens_in: int | None = None
    tokens_out: int | None = None
    tokens_reasoning: int | None = None
    cached_in: int | None = None


@dataclass(frozen=True)
class TextChunk:
    text: str


@dataclass(frozen=True)
class StepComplete:
    """Exactly one of these ends a stream. `finish_reason` is one of
    'stop' | 'tool_calls' | 'length' | 'content_filter'."""

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str = "stop"
    usage: Usage | None = None
    structured_data: Any = None
    model_id: str | None = None
    provider_id: str | None = None
    raw: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ModelSwitched:
    """One candidate failed and the next is taking over — never silent."""

    to_model_id: str
    reason: str
    from_model_id: str | None = None


@dataclass(frozen=True)
class ErrorEvent:
    """A terminal failure, in words the person can read."""

    kind: str
    message: str


#: What a model client yields, in order: any number of chunks (possibly
#: interrupted by a switch), then exactly one completed step.
ModelEvent = TextChunk | StepComplete | ModelSwitched | ErrorEvent


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
        model_id: str | None = None,
        role: str | None = None,
        need: dict[str, bool] | None = None,
    ) -> Iterator[ModelEvent]:
        """`need` is what this turn REQUIRES — vision, video, audio, web
        search. It is the orchestrator's own knowledge: it is the side that
        knows an image was attached.

        `role` is a plain string ('conversation', 'voice', 'control',
        'background', 'utility') describing what kind of turn this is, since
        the orchestrator is the side that knows whether it was spoken,
        scheduled or typed.
        """
        ...


class NoModelClient:
    """The `ModelClient` used while no model system exists: every call fails
    with `ModelUnavailable`, which the turn loop already turns into a plain
    "no model" failure rather than a crash."""

    def stream(self, **_: Any) -> Iterator[ModelEvent]:
        raise ModelUnavailable(NO_MODEL_MESSAGE)
        yield  # pragma: no cover — makes this a generator, like a real client
