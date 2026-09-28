"""The descriptive fields the OpenAI-compatible gateways share: what a model takes in
and gives out, whether tool calling works, what it costs. Read only as reported, and
each written down either way it was reported — `False` is the gateway SAYING no,
which is how a later listing corrects an earlier one. A field it left out is left out.
"""

from __future__ import annotations

from typing import Any

#: Kinds of model a gateway can list that never take a chat turn.
NOT_CHAT_TYPES = {"video", "image", "audio", "speech", "tts", "stt", "transcription",
                  "embedding", "embeddings", "rerank", "moderation"}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def modalities(row: dict[str, Any]) -> tuple[list[str] | None, list[str] | None]:
    """`(inputs, outputs)` — at the top level or under `architecture`, where given."""
    arch = _dict(row.get("architecture"))
    inputs = row.get("input_modalities") or arch.get("input_modalities")
    outputs = row.get("output_modalities") or arch.get("output_modalities")
    return (inputs if isinstance(inputs, list) and inputs else None,
            outputs if isinstance(outputs, list) and outputs else None)


def describe(row: dict[str, Any]) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    inputs, outputs = modalities(row)
    kind = row.get("type")
    if (isinstance(kind, str) and kind.lower() in NOT_CHAT_TYPES) or (outputs and "text" not in outputs):
        facts["chat"] = False
    elif outputs:
        facts["chat"] = True
    caps = _dict(row.get("capabilities"))
    supported = row.get("supported_parameters")
    if isinstance(caps.get("tool_calling"), bool):
        facts["tools"] = caps["tool_calling"]
    elif isinstance(supported, list) and supported:
        facts["tools"] = "tools" in supported
    if inputs:
        facts["image"] = "image" in inputs
    # A price, where the gateway states one. Kept because "needs credit" is a fact about
    # paid models only; a model it lists as free keeps working on an account with none.
    pricing = _dict(row.get("pricing"))
    try:
        prices = [float(pricing[k]) for k in ("prompt", "completion") if k in pricing]
    except (TypeError, ValueError):
        prices = []
    if prices:
        facts["free"] = all(p == 0 for p in prices)
    return facts
