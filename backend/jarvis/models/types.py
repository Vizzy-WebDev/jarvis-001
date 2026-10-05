"""The shapes every part of the model layer speaks: a request, the items in a
conversation, a response, and the events of a stream.

Plain frozen dataclasses and nothing else. Nothing here knows a provider, a
connection or a wire format. Input and output use the SAME item types, so an
answer can be put straight back into the next request unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Union

DATA_CLASSES = ("public", "personal", "sensitive")
TRUST_CLASSES = ("local", "zero_retention", "standard")
OPTIMIZE = ("quality", "cost", "speed")
EFFORTS = ("none", "low", "medium", "high")
STOP_REASONS = ("stop", "tool_calls", "length", "content_filter")

DataClass = Literal["public", "personal", "sensitive"]
Optimize = Literal["quality", "cost", "speed"]
Effort = Literal["none", "low", "medium", "high"]


# --- items -----------------------------------------------------------------------------

@dataclass(frozen=True)
class TextPart:
    text: str


@dataclass(frozen=True)
class ImagePart:
    mime: str
    data_b64: str


Part = Union[TextPart, ImagePart]


@dataclass(frozen=True)
class Message:
    role: Literal["user", "assistant"]
    parts: tuple[Part, ...]

    @property
    def text(self) -> str:
        return "".join(p.text for p in self.parts if isinstance(p, TextPart))


@dataclass(frozen=True)
class ToolCall:
    """One tool the model asked to run. `id` is canonical — made by the layer, never
    a provider's. `arguments` is None only when the model's arguments were not valid
    JSON; they are then in `raw_arguments`, untouched, and a warning says so."""

    id: str
    name: str
    arguments: Mapping[str, Any] | None
    raw_arguments: str | None = None


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    name: str
    content: Any
    is_error: bool = False


@dataclass(frozen=True)
class Sealed:
    """Provider state the layer carries but never reads: a thinking block and its
    signature, encrypted reasoning, a thought signature, the provider's own ids for
    this layer's tool calls. Tagged with the endpoint that produced it, and only ever
    sent back to that endpoint; anywhere else it is dropped and reported."""

    endpoint_id: str
    kind: str
    payload: Any = field(hash=False, compare=True)


Item = Union[Message, ToolCall, ToolResult, Sealed]


# --- the request -----------------------------------------------------------------------

@dataclass(frozen=True)
class Section:
    """One labelled piece of the instructions (persona, rules, context, task...)."""

    label: str
    text: str


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: Mapping[str, Any]  # plain JSON Schema


@dataclass(frozen=True)
class OutputSpec:
    kind: Literal["text", "json"] = "text"
    schema: Mapping[str, Any] | None = None
    enforcement: Literal["required", "best_effort"] = "required"


TEXT = OutputSpec()


@dataclass(frozen=True)
class Requirements:
    capabilities: frozenset[str] = frozenset()
    min_context: int | None = None
    max_cost: float | None = None  # US dollars, for this one call
    allow_family_change: bool = True
    pin: str | None = None  # an alias name, never a model or provider name


@dataclass(frozen=True)
class Hints:
    stable_prefix_until: str | None = None  # the label of the last stable Section
    reasoning_effort: Effort | None = None
    max_output_tokens: int | None = None


@dataclass(frozen=True)
class Request:
    task_class: str
    data_class: DataClass
    items: tuple[Item, ...]
    instructions: tuple[Section, ...] = ()
    tools: tuple[Tool, ...] = ()
    output: OutputSpec = TEXT
    requirements: Requirements = Requirements()
    optimize: Optimize | None = None  # None: the task class's route decides
    prefer: tuple[str, ...] = ()  # alias names
    hints: Hints = Hints()
    affinity_key: str | None = None
    extensions: Mapping[str, Mapping[str, Any]] = field(default_factory=dict, hash=False)

    def __post_init__(self) -> None:
        if not self.task_class:
            raise ValueError("A request needs a task_class.")
        if self.data_class not in DATA_CLASSES:
            raise ValueError(f"data_class must be one of {', '.join(DATA_CLASSES)}.")
        if self.optimize is not None and self.optimize not in OPTIMIZE:
            raise ValueError(f"optimize must be one of {', '.join(OPTIMIZE)}.")
        if self.hints.reasoning_effort is not None and self.hints.reasoning_effort not in EFFORTS:
            raise ValueError(f"reasoning_effort must be one of {', '.join(EFFORTS)}.")
        if self.output.kind == "json" and self.output.schema is None:
            raise ValueError("A JSON output needs a schema.")
        if self.hints.stable_prefix_until is not None and \
                self.hints.stable_prefix_until not in {s.label for s in self.instructions}:
            raise ValueError("stable_prefix_until must name one of the instruction sections.")


# --- the response ----------------------------------------------------------------------

@dataclass(frozen=True)
class Usage:
    """Normalized: `input` is ALL input tokens, `cached` the part served from a cache.
    `None` means the provider did not say — never a zero standing in for it."""

    input: int | None = None
    output: int | None = None
    cached: int | None = None
    reasoning: int | None = None
    cost: float | None = None  # estimated, US dollars; None when a price is unknown
    raw: Mapping[str, Any] = field(default_factory=dict, hash=False)


@dataclass(frozen=True)
class Attempt:
    endpoint_id: str
    error_type: str | None  # None: it answered
    message: str | None = None
    ms: int | None = None


@dataclass(frozen=True)
class Fallback:
    from_endpoint: str
    to_endpoint: str
    reason: str


@dataclass(frozen=True)
class Provenance:
    endpoint_id: str
    reported_model: str | None
    attempts: tuple[Attempt, ...] = ()
    fallbacks: tuple[Fallback, ...] = ()


FeatureState = Literal["native", "emulated", "dropped"]


@dataclass(frozen=True)
class FeatureReport:
    features: Mapping[str, FeatureState] = field(default_factory=dict, hash=False)
    warnings: tuple[str, ...] = ()
    #: A hint sent as something other than what was asked: {"reasoning_effort": {"asked", "sent"}}.
    mapped: Mapping[str, Mapping[str, str]] = field(default_factory=dict, hash=False)


@dataclass(frozen=True)
class Response:
    items: tuple[Item, ...]
    stop_reason: str
    usage: Usage
    provenance: Provenance
    report: FeatureReport
    #: For a JSON output: the parsed, validated value. None for text.
    data: Any = None
    request_id: str = ""

    @property
    def text(self) -> str:
        return "".join(i.text for i in self.items if isinstance(i, Message))

    @property
    def tool_calls(self) -> tuple[ToolCall, ...]:
        return tuple(i for i in self.items if isinstance(i, ToolCall))


# --- stream events ---------------------------------------------------------------------

@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolCallStarted:
    id: str
    name: str


@dataclass(frozen=True)
class ToolArgsDelta:
    id: str
    delta: str


@dataclass(frozen=True)
class ToolCallCompleted:
    call: ToolCall


@dataclass(frozen=True)
class ReasoningDelta:
    text: str


@dataclass(frozen=True)
class UsageEvent:
    usage: Usage


@dataclass(frozen=True)
class WarningEvent:
    message: str


@dataclass(frozen=True)
class ErrorEvent:
    error: Any  # a ModelError


@dataclass(frozen=True)
class Done:
    response: Response


Event = Union[TextDelta, ToolCallStarted, ToolArgsDelta, ToolCallCompleted, ReasoningDelta,
              UsageEvent, WarningEvent, ErrorEvent, Done]

#: The events that carry content. "No fallback after the first event" counts these.
CONTENT_EVENTS = (TextDelta, ToolCallStarted, ToolArgsDelta, ToolCallCompleted, ReasoningDelta)


# --- what a driver yields --------------------------------------------------------------

@dataclass(frozen=True)
class SealedEvent:
    """A driver produced provider state to carry (it becomes a `Sealed` output item,
    in the position it arrived)."""

    kind: str
    payload: Any


@dataclass(frozen=True)
class Finish:
    """The last thing a driver yields: how it ended, and what the server reported."""

    stop_reason: str
    usage: Usage
    reported_model: str | None = None


DriverEvent = Union[TextDelta, ToolCallStarted, ToolArgsDelta, ToolCallCompleted, ReasoningDelta,
                    SealedEvent, Finish]


# --- explaining a route ----------------------------------------------------------------

@dataclass(frozen=True)
class Rejection:
    endpoint_id: str
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class Explanation:
    ranked: tuple[str, ...]
    rejected: tuple[Rejection, ...]
    #: Endpoints that stay eligible only by emulating structured output.
    emulated: tuple[str, ...] = ()
