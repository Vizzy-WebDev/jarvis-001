"""What every wire-format adapter must provide, and the small amount of logic
worth sharing across all three rather than copied into each.

An adapter TRANSLATES; it never decides. It is handed a resolved
`ReasoningRequest` (already clamped to what this model can take), an
already-filtered parameter dict (already stripped of anything this model is
confirmed not to accept), and the caller's messages — its only job is turning
those into one wire-format's actual request, sending it, and normalizing
whatever comes back into this system's own `StreamEvent`s. Capability
decisions, routing, and retries all happen one layer up.
"""

from __future__ import annotations

from typing import Any, Iterator, Mapping, Protocol, runtime_checkable

from ..reasoning import Effort, ReasoningKind, ReasoningRequest
from ..request import (
    AIRequest, Message, ResponseFormat, StreamEvent, ToolDefinition,
)


class AdapterError(RuntimeError):
    """A failure `ai/errors.py`'s classifier should see. The provider's own
    exception is kept as `__cause__` automatically by `raise ... from err`."""


@runtime_checkable
class ProviderAdapter(Protocol):
    NAME: str

    def stream(
        self,
        provider: Any,             # providers.Provider — typed loosely to avoid an import cycle
        native_model_id: str,
        messages: tuple[Message, ...],
        system: str,
        tools: tuple[ToolDefinition, ...],
        *,
        reasoning: ReasoningRequest | None,
        params: Mapping[str, Any],
        response_format: ResponseFormat | None,
    ) -> Iterator[StreamEvent]:
        """Send one request and yield normalized events, ending in exactly one
        `Completed`. May raise `AdapterError` (or let the SDK's own exception
        propagate) — the caller classifies it via `ai/errors.py`, never this
        function."""
        ...

    def test_connection(self, provider: Any, native_model_id: str) -> dict[str, Any]:
        """Prove GENERATION, not just that the address answers — `{"ok": bool,
        "error": str | None}`. Listing models proves an endpoint exists and a
        key is accepted for listing; it does not prove this model can
        actually produce a token."""
        ...

    def discover_models(self, provider: Any) -> list[dict[str, Any]]:
        """What this provider says it has, in this adapter's own vocabulary —
        `ai/discovery.py` normalizes it from here."""
        ...

    def friendly_error(self, err: BaseException) -> str:
        ...


def model_id_for(native_model_id: str, reasoning: ReasoningRequest | None) -> str:
    """The id to actually put on the wire.

    Almost always the model's own. The exception is a `VARIANT` reasoning
    scheme, where a provider expresses "think harder" by publishing a
    SEPARATE model id rather than accepting a parameter — wire-agnostic, so
    it lives here rather than duplicated in three adapters.
    """
    if reasoning is not None and reasoning.kind is ReasoningKind.VARIANT and reasoning.native:
        return str(reasoning.native)
    return native_model_id


def text_of(message: Message) -> str:
    return message.text or ""
