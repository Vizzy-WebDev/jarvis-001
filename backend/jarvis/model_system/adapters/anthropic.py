"""Anthropic — the Messages API wire format.

Two things confirmed against the real, installed SDK rather than assumed:

* **`thinking` takes a token budget**, via `{"type": "enabled",
  "budget_tokens": N}`. A `TIERS`/`VARIANT` scheme is skipped rather than
  converted into some invented number of tokens — guessing what a tier is
  "worth" here would be inventing the one thing the registry exists to stop
  being invented.
* **A raw assistant turn's own content blocks round-trip verbatim** when the
  caller supplies one (`Message.raw["content"]`) — rebuilding it from plain
  text loses information Anthropic expects back unchanged on a multi-step
  tool-calling turn.
"""

from __future__ import annotations

from typing import Any, Iterator, Mapping

from ..reasoning import ReasoningKind, ReasoningRequest
from ..request import (
    Completed, Message, ResponseFormat, StreamEvent, TextDelta, ToolCall, ToolDefinition,
    Usage,
)
from .base import AdapterError, model_id_for

NAME = "anthropic"
#: See `adapters/gemini.py`'s own flag for what this means.
SUPPORTS_REALTIME = False

MAX_TOKENS = 4096
REQUEST_TIMEOUT_S = 120.0


class NoCredential(AdapterError):
    pass


def _client(provider: Any):
    from anthropic import Anthropic

    from ..credentials import resolve

    key = resolve(provider.credential_ref)
    if not key:
        raise NoCredential("No API key configured.")
    # max_retries=0: retry policy belongs one layer up (ai/fallback.py), not
    # to each SDK — a hidden SDK-level retry would swallow exactly the
    # failure the gateway needs to see to bench a model and try the next.
    return Anthropic(api_key=key, base_url=provider.base_url or None,
                     max_retries=0, timeout=REQUEST_TIMEOUT_S)


def _media_blocks(attachments) -> list[dict[str, Any]]:
    blocks = []
    for a in attachments or ():
        if a.modality.value != "image" or not a.data:
            continue
        import base64
        blocks.append({"type": "image", "source": {
            "type": "base64", "media_type": a.mime_type or "image/png",
            "data": base64.b64encode(a.data).decode("ascii")}})
    return blocks


def to_wire(messages: tuple[Message, ...]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "user" and m.attachments:
            content = ([{"type": "text", "text": m.text}] if m.text else [])
            out.append({"role": "user", "content": content + _media_blocks(m.attachments)})
        elif m.role == "user" and m.text:
            out.append({"role": "user", "content": m.text})
        elif m.role == "assistant":
            raw = m.raw or {}
            if raw.get("adapter") == NAME and raw.get("content"):
                out.append({"role": "assistant", "content": raw["content"]})
            elif m.tool_calls:
                content = ([{"type": "text", "text": m.text}] if m.text else [])
                content += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.args}
                           for c in m.tool_calls]
                out.append({"role": "assistant", "content": content})
            elif m.text:
                out.append({"role": "assistant", "content": m.text})
        elif m.role == "tool" and m.tool_call_id:
            out.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.text}]})
    return out


def _system_blocks(system: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}] if system else []


def _tools(tools: tuple[ToolDefinition, ...]) -> list[dict[str, Any]]:
    return [{"name": t.name, "description": t.description,
             "input_schema": t.parameters or {"type": "object", "properties": {}}}
            for t in tools]


def _thinking(reasoning: ReasoningRequest | None) -> dict[str, Any] | None:
    if reasoning is None or reasoning.kind is not ReasoningKind.BUDGET:
        return None
    budget = reasoning.native
    if not budget:
        return None
    return {"type": "enabled", "budget_tokens": int(budget)}


def _generation_kwargs(params: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "temperature" in params:
        out["temperature"] = params["temperature"]
    if "top_p" in params:
        out["top_p"] = params["top_p"]
    if "top_k" in params:
        out["top_k"] = params["top_k"]
    if "stop_sequences" in params:
        out["stop_sequences"] = list(params["stop_sequences"])
    return out


def _usage(usage: Any) -> Usage | None:
    if usage is None:
        return None
    tokens_in = getattr(usage, "input_tokens", None)
    tokens_out = getattr(usage, "output_tokens", None)
    cached_in = getattr(usage, "cache_read_input_tokens", None)
    if tokens_in is None and tokens_out is None and cached_in is None:
        return None
    return Usage(tokens_in=tokens_in, tokens_out=tokens_out, cached_in=cached_in)


def stream(
    provider: Any,
    native_model_id: str,
    messages: tuple[Message, ...],
    system: str,
    tools: tuple[ToolDefinition, ...],
    *,
    reasoning: ReasoningRequest | None,
    params: Mapping[str, Any],
    response_format: ResponseFormat | None,
) -> Iterator[StreamEvent]:
    client = _client(provider)
    thinking = _thinking(reasoning)
    max_tokens = params.get("max_output_tokens") or MAX_TOKENS

    with client.messages.stream(
        model=model_id_for(native_model_id, reasoning),
        max_tokens=max_tokens,
        system=_system_blocks(system),
        messages=to_wire(messages),
        tools=_tools(tools),
        **_generation_kwargs(params),
        **({"thinking": thinking} if thinking else {}),
    ) as running:
        for event in running:
            if (getattr(event, "type", None) == "content_block_delta"
                    and getattr(event.delta, "type", None) == "text_delta"):
                yield TextDelta(event.delta.text)
        message = running.get_final_message()

    usage = _usage(getattr(message, "usage", None))
    blocks = [b.model_dump() if hasattr(b, "model_dump") else dict(b) for b in message.content]
    uses = [b for b in blocks if b.get("type") == "tool_use"]
    raw = {"adapter": NAME, "content": blocks}
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")

    if message.stop_reason == "tool_use" and uses:
        yield Completed(
            text=text,
            tool_calls=tuple(ToolCall(b["id"], b["name"], b.get("input") or {}) for b in uses),
            finish_reason="tool_calls", usage=usage, raw=raw,
        )
        return

    if not text:
        raise AdapterError("The model returned an empty response — try again later.")
    yield Completed(text=text, finish_reason="stop", usage=usage, raw=raw)


def test_connection(provider: Any, native_model_id: str) -> dict[str, Any]:
    try:
        client = _client(provider)
        message = client.messages.create(
            model=native_model_id, max_tokens=32,
            messages=[{"role": "user", "content": 'Say "ready" and nothing else.'}])
        text = "".join(getattr(b, "text", "") for b in message.content)
        if not text:
            return {"ok": False, "error": "The server responded but with no text — check the model name."}
        return {"ok": True, "error": None}
    except NoCredential:
        return {"ok": False, "error": "That connection needs an API key."}
    except Exception as err:  # noqa: BLE001
        return {"ok": False, "error": friendly_error(err)}


def discover_models(provider: Any) -> list[dict[str, Any]]:
    client = _client(provider)
    return [{"model": m.id, "label": getattr(m, "display_name", None)}
            for m in client.models.list()]


def friendly_error(err: BaseException) -> str:
    body = getattr(err, "body", None)
    if isinstance(body, dict):
        inner = body.get("error")
        if isinstance(inner, dict) and isinstance(inner.get("message"), str):
            return inner["message"]
    return str(err) or "That connection didn't work."
