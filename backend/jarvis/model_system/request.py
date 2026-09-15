"""The one normalized request/response shape every caller uses (§18-20).

A caller builds an `AIRequest` and hands it to `model_system.gateway.execute()`. It never
knows, and never needs to know, which provider answers — that is resolved
inside the gateway from `requirements` (hard, eliminates candidates) and
`preferences` (soft, ranks them). See `model_system/router.py` for the line between the
two: **requirements eliminate; preferences rank.**

Streaming and non-streaming answer through the same normalized shapes:
`AIResponse` is what a non-streaming caller gets back; a streaming caller gets
the same facts spread across `StreamEvent`s, ending in exactly one `Completed`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from .parameters import GenerationParams
from .reasoning import Effort


class Modality(Enum):
    """One kind of content a message can carry (input) or a request can ask
    for back (output). A fact about the REQUEST — what an attachment IS, what
    a caller is asking to send or receive — never a claim about what a model
    can handle; that three-state question lives on `model_system/capabilities.py`'s
    `Capabilities` instead, keyed by the matching capability name (`IMAGE` ->
    `vision`, and so on — see `model_system/router.py`)."""

    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    FILE = "file"
    EMBEDDING = "embedding"


class Role(Enum):
    """What kind of turn this is, in routing terms — a per-request
    classification the caller attaches, never a stored preference. Each
    corresponds to something `model_system/router.py` genuinely weighs differently."""

    #: A person is waiting for the answer right now.
    CONVERSATION = "conversation"
    #: Spoken. Latency is most of the experience.
    VOICE = "voice"
    #: Driving the screen. A wrong step costs more than a slightly clumsy one.
    CONTROL = "control"
    #: Scheduled work, nobody waiting. Cost matters more than latency.
    BACKGROUND = "background"
    #: Small, frequent, one-off asks — extraction, verification, triage.
    UTILITY = "utility"

    @property
    def background_work(self) -> bool:
        return self in (Role.BACKGROUND, Role.UTILITY)


def role_from(value: Any) -> Role:
    """A `Role` from whatever a caller is holding. An unrecognised value is not
    an error — a turn nobody classified is an ordinary conversation, and
    failing it over a typo would be a routing decision made by accident."""
    if isinstance(value, Role):
        return value
    try:
        return Role(str(value).strip().lower())
    except ValueError:
        return Role.CONVERSATION


@dataclass(frozen=True)
class Attachment:
    """One piece of non-text content riding on a message."""

    modality: Modality
    mime_type: str
    data: bytes | None = None
    uri: str | None = None


@dataclass(frozen=True)
class ToolCall:
    """One tool a model asked to run. `id` is the model's own correlation id —
    handed back unchanged when the caller supplies the result."""

    id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolDefinition:
    """One tool made available to the model. Tool CALLING is a model
    capability; tool EXECUTION is never this system's job — a caller receives
    a `ToolCall` and decides what to do with it, then supplies the result back
    as a `Message` with `role="tool"`."""

    name: str
    description: str
    parameters: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Message:
    role: str  # "system" | "user" | "assistant" | "tool"
    #: For role="tool", this is the SERIALIZED result — already a plain
    #: string an adapter can put on its wire as-is (or parse back, when its
    #: wire wants an object rather than a string — see `adapters/gemini.py`).
    #: Serializing once, here, is what keeps three adapters from disagreeing
    #: about how a tool's return value becomes text.
    text: str = ""
    attachments: tuple[Attachment, ...] = ()
    #: Set on a role="tool" message: which call this is the result of.
    tool_call_id: str | None = None
    #: Set on a role="tool" message: the NAME of the tool that was called.
    #: Some wire formats (Gemini) correlate a function response by name
    #: rather than by call id, so both have to travel with the message.
    tool_name: str | None = None
    #: Set on a role="assistant" message that made calls the transcript replays.
    tool_calls: tuple[ToolCall, ...] = ()
    #: The provider's own reply object, when a provider needs exact
    #: round-tripping (Gemini's `thought_signature` must return verbatim or
    #: the next call is rejected). Never inspected outside the adapter that
    #: wrote it.
    raw: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ResponseFormat:
    """A structured/JSON-schema output request (§22). `strict`, where a
    provider offers it, means the schema is enforced rather than merely
    requested. Detecting whether a model can actually honour this at all is
    `model_system/capabilities.py`'s `structured_output` flag — this dataclass only
    carries what was asked for."""

    schema: Mapping[str, Any]
    name: str = "response"
    strict: bool = True


@dataclass(frozen=True)
class Requirements:
    """What this request HARD-requires. Eliminates incompatible candidates and
    ranks nothing — see `model_system/router.py`."""

    modalities_in: frozenset[Modality] = frozenset()
    needs_tools: bool = False
    needs_structured_output: bool = False
    needs_streaming: bool = False
    #: Capability name -> required. Only a confirmed `NO` on a candidate
    #: excludes it; see `model_system/capabilities.py` and `model_system/router.py`.
    capabilities: Mapping[str, bool] = field(default_factory=dict)
    min_context_tokens: int = 0
    estimated_input_tokens: int = 0


@dataclass(frozen=True)
class Preferences:
    """What this request would LIKE. Ranks compatible candidates and
    eliminates nothing."""

    role: Role = Role.CONVERSATION
    #: 'fast' | 'balanced' | 'quality'. `None` reads the app-wide dial.
    balance: str | None = None
    preferred_provider_id: str | None = None
    preferred_model_id: str | None = None


@dataclass(frozen=True)
class AIRequest:
    """The one shape every caller builds, regardless of which provider answers."""

    messages: tuple[Message, ...]
    system: str = ""
    tools: tuple[ToolDefinition, ...] = ()
    tool_choice: Any = None
    params: GenerationParams = field(default_factory=GenerationParams)
    reasoning: Effort | None = None
    response_format: ResponseFormat | None = None
    requirements: Requirements = field(default_factory=Requirements)
    preferences: Preferences = field(default_factory=Preferences)
    stream: bool = True
    #: A specific model pinned by id — the Default case. `None` is Auto: the
    #: router chooses. See `model_system/router.py`.
    model_id: str | None = None
    profile_id: str | None = None
    session_id: str | None = None
    #: Assigned by the gateway when absent — see `model_system/gateway.py`. Set by the
    #: caller only to correlate an existing id (e.g. a resumed job).
    request_id: str | None = None
    background: bool = False


@dataclass(frozen=True)
class Usage:
    """What a provider itself reported this call consumed. A field left
    `None` means the provider did not report it — never filled with a zero
    that would misrepresent "not measured" as "measured as nothing"."""

    tokens_in: int | None = None
    tokens_out: int | None = None
    tokens_reasoning: int | None = None
    cached_in: int | None = None


# --- streaming events (§20) --------------------------------------------------

@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolCallStarted:
    id: str
    name: str


@dataclass(frozen=True)
class ToolCallArgsDelta:
    """A partial chunk of one tool call's argument JSON, in the provider's own
    incremental shape. Optional — an adapter that only knows the complete call
    when it is done may skip straight to `ToolCallReady`."""

    id: str
    delta: str


@dataclass(frozen=True)
class ToolCallReady:
    call: ToolCall


@dataclass(frozen=True)
class Completed:
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
    """A candidate failed and the next one is taking over — never silent, see
    `model_system/fallback.py`."""

    to_model_id: str
    reason: str
    from_model_id: str | None = None


@dataclass(frozen=True)
class ErrorEvent:
    """A terminal failure, normalized per `model_system/errors.py`."""

    kind: str
    message: str


StreamEvent = (
    TextDelta | ToolCallStarted | ToolCallArgsDelta | ToolCallReady
    | Completed | ModelSwitched | ErrorEvent
)


@dataclass(frozen=True)
class AIResponse:
    """The non-streaming convenience shape — the same facts a stream ends
    with, collected into one value."""

    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str = "stop"
    usage: Usage | None = None
    model_id: str | None = None
    provider_id: str | None = None
    request_id: str | None = None
    structured_data: Any = None
    raw: Mapping[str, Any] | None = None
