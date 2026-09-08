"""What every wire format must provide, and what it may carry.

One adapter per WIRE FORMAT, not per provider: `openai_compatible` alone covers
OpenAI, Ollama, LM Studio, OpenRouter, Groq and anything else OpenAI-shaped, by
pointing `baseUrl` elsewhere. All three accept a custom base URL, so a Custom
connection resolved to any of the three shapes can actually be called.

**On CAPABILITIES (§26).** In the Node implementation the adapter's capability
table is a hard GATE: `ai.js` refuses to route a video request to any model whose
adapter does not declare `video`, which makes video, audio and web search
permanently Gemini-only no matter what the user knows about their own models.

Here it is a SEED, not a gate. A model's stored `caps` — seeded from this table
when the model is added, editable by the user afterwards — is what routing reads.
If an adapter genuinely cannot carry what a model claims, the call fails and is
classified `unsupported`, which benches that one model for that one thing.
Getting it wrong costs one failed call; the alternative cost a whole feature.
"""

from __future__ import annotations

from typing import Any, Iterator, Protocol, runtime_checkable

from ..orchestrator.model_port import ModelEvent


class AdapterError(RuntimeError):
    """A failure the classifier should see, with the provider's own error kept
    as `__cause__` so status and message text survive."""


@runtime_checkable
class Adapter(Protocol):
    name: str
    CAPABILITIES: dict[str, bool]

    def stream(
        self,
        entry: dict[str, Any],
        messages: list[dict[str, Any]],
        *,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> Iterator[ModelEvent]:
        ...

    def test_connection(self, entry: dict[str, Any]) -> dict[str, Any]:
        ...

    def list_models(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        ...

    def friendly_error(self, err: BaseException) -> str:
        ...


def text_of(message: dict[str, Any]) -> str:
    value = message.get("text")
    return value if isinstance(value, str) else ""
