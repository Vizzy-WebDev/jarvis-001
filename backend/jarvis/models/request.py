"""One model request, typed — what every provider module is handed.

The conversation is stored as plain dicts (`jarvis.conversation`), in Jarvis's own
shape. `conversation.to_chat_request` turns that into this, once, so no provider
module ever reads a stored message dict or knows how Jarvis keeps its history.

A leaf on purpose: it imports nothing of Jarvis, so the conversation store and every
provider can import it without a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Union

#: The reasoning levels a request can ask for, in increasing order of effort. Neutral on
#: purpose: each provider module maps them onto its own API's names (or budgets), and
#: a model is only ever sent a level its provider reported it accepts.
REASONING_LEVELS = ("minimal", "balanced", "thorough", "maximum")

Reasoning = Literal["minimal", "balanced", "thorough", "maximum"]


@dataclass(frozen=True)
class TextPart:
    text: str


@dataclass(frozen=True)
class ImagePart:
    mime_type: str
    data_base64: str


@dataclass(frozen=True)
class MediaPart:
    """An attachment that is not an image (audio, video, a document). Most formats
    refuse these, in words; the ones that take them pass the bytes through."""

    kind: str
    mime_type: str
    data_base64: str


@dataclass(frozen=True)
class ToolCallPart:
    id: str
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class ToolResultPart:
    call_id: str
    name: str
    #: What the tool returned — any JSON-able value. Each format serialises it its own way.
    result: Any
    is_error: bool = False


Part = Union[TextPart, ImagePart, MediaPart, ToolCallPart, ToolResultPart]


@dataclass(frozen=True)
class Message:
    role: Literal["user", "assistant", "tool", "system"]
    parts: tuple[Part, ...] = ()
    #: An assistant reply exactly as its provider sent it — `{"adapter": <format>, ...}` —
    #: for the formats that must be handed their own content back verbatim (Anthropic's
    #: signed thinking blocks, Gemini's thought signatures). Only the module whose format
    #: it names ever reads it; every other module uses `parts`. `None` when there is
    #: nothing to replay, including a reply that was cut off part-way.
    replay: dict[str, Any] | None = None

    def text(self) -> str:
        return "".join(p.text for p in self.parts if isinstance(p, TextPart))

    def of(self, kind: type) -> list[Any]:
        return [p for p in self.parts if isinstance(p, kind)]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    #: A JSON Schema object for the tool's arguments.
    parameters: dict[str, Any]


@dataclass(frozen=True)
class GenerationOptions:
    """How to generate, as distinct from what to answer. Every field is optional, and a
    provider module that can't honour one drops it — never refuses the turn over it."""

    reasoning: Reasoning | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None
    tool_choice: Literal["auto", "required", "none"] | None = None
    response_format: Literal["text", "json"] | None = None


@dataclass(frozen=True)
class ChatRequest:
    messages: tuple[Message, ...]
    system: str | None = None
    tools: tuple[ToolSpec, ...] = ()
    options: GenerationOptions = field(default_factory=GenerationOptions)
    #: The provider's own id for the model, verbatim.
    model_id: str = ""

    def has_images(self) -> bool:
        return any(isinstance(p, ImagePart) for m in self.messages for p in m.parts)
