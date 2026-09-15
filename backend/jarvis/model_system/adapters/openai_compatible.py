"""The "any OpenAI-shaped server" adapter — OpenAI itself, Ollama, LM Studio,
OpenRouter, Groq, Together, and anything else that speaks this wire format,
by pointing `provider.base_url` elsewhere. See `adapters/__init__.py` for why
this is deliberately one file rather than one per brand.

Two behaviours here were found live, not designed, and are worth keeping:

* **A 200 OK can carry an error payload as ordinary content.** A gateway
  whose own upstream pool had failed once returned
  `{"error":{"message":"[429] ... Rate limit exceeded"}}` as the assistant's
  message body. Undetected, that text streams to the caller and the turn
  records as a success. So a reply that STARTS with `{` is held back briefly
  and inspected; anything else streams normally from the first character.
* **An empty final response is a failure, not an empty success** — yielding
  a placeholder would mark a broken model healthy and show the caller
  nothing.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterator, Mapping

from ..request import (
    Completed, Message, ResponseFormat, StreamEvent, TextDelta, ToolCall, ToolDefinition,
    Usage,
)
from .base import AdapterError, model_id_for

NAME = "openai_compatible"
#: See `adapters/gemini.py`'s own flag for what this means.
SUPPORTS_REALTIME = False

REQUEST_TIMEOUT_S = 120.0
#: How much of a reply starting with '{' is held before deciding it is not an
#: error blob. Real ones seen in practice are far shorter than this.
ERROR_PAYLOAD_HOLD_CHARS = 500


class NoCredential(AdapterError):
    pass


def _client(provider: Any):
    from openai import OpenAI  # imported lazily: a missing SDK is a startup concern, not an import-time one

    from ..credentials import resolve

    key = resolve(provider.credential_ref)
    if provider.key_required and not key:
        raise NoCredential("No API key configured.")
    # max_retries=0: the SDK retries 429s/5xxs by default, which would
    # silently swallow exactly the failure the gateway needs to see in order
    # to bench a model and move to the next candidate.
    return OpenAI(api_key=key or "not-needed", base_url=provider.base_url or None,
                  max_retries=0, timeout=REQUEST_TIMEOUT_S)


def _error_payload_message(text: str) -> str | None:
    trimmed = (text or "").strip()
    if not trimmed.startswith("{"):
        return None
    try:
        parsed = json.loads(trimmed)
    except ValueError:
        return None
    message = (parsed or {}).get("error", {})
    message = message.get("message") if isinstance(message, dict) else None
    return message if isinstance(message, str) and message else None


def _media_parts(attachments) -> list[dict[str, Any]]:
    """Images only — this format has no video part type, so video is dropped
    rather than mislabelled as something it is not."""
    parts = []
    for a in attachments or ():
        if a.modality.value != "image":
            continue
        if a.data:
            import base64
            url = f"data:{a.mime_type or 'image/png'};base64,{base64.b64encode(a.data).decode('ascii')}"
        else:
            url = a.uri
        if url:
            parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts


def to_wire(messages: tuple[Message, ...], system: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in messages:
        if m.role == "user" and m.attachments:
            content = ([{"type": "text", "text": m.text}] if m.text else [])
            out.append({"role": "user", "content": content + _media_parts(m.attachments)})
        elif m.role == "user" and m.text:
            out.append({"role": "user", "content": m.text})
        elif m.role == "assistant":
            if m.tool_calls:
                out.append({
                    "role": "assistant", "content": m.text or None,
                    "tool_calls": [
                        {"id": c.id, "type": "function",
                         "function": {"name": c.name, "arguments": json.dumps(c.args)}}
                        for c in m.tool_calls],
                })
            elif m.text:
                out.append({"role": "assistant", "content": m.text})
        elif m.role == "tool" and m.tool_call_id:
            out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.text})
    return out


def _tools(tools: tuple[ToolDefinition, ...]) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    return [{"type": "function",
             "function": {"name": t.name, "description": t.description,
                          "parameters": t.parameters or {"type": "object", "properties": {}}}}
            for t in tools]


def _response_format(response_format: ResponseFormat | None) -> dict[str, Any] | None:
    if response_format is None:
        return None
    return {"type": "json_schema", "json_schema": {
        "name": response_format.name, "schema": dict(response_format.schema),
        "strict": response_format.strict}}


def _generation_kwargs(params: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for wire, key in (("temperature", "temperature"), ("top_p", "top_p"),
                      ("max_tokens", "max_output_tokens"), ("stop", "stop_sequences"),
                      ("frequency_penalty", "frequency_penalty"),
                      ("presence_penalty", "presence_penalty"), ("seed", "seed"),
                      ("tool_choice", "tool_choice"),
                      ("parallel_tool_calls", "parallel_tool_calls"),
                      ("reasoning_effort", "reasoning")):
        if key in params:
            value = params[key]
            out[wire] = list(value) if key == "stop_sequences" else value
    return out


def _usage(usage: Any) -> Usage | None:
    if usage is None:
        return None
    tokens_in = getattr(usage, "prompt_tokens", None)
    tokens_out = getattr(usage, "completion_tokens", None)
    details = getattr(usage, "prompt_tokens_details", None)
    cached_in = getattr(details, "cached_tokens", None) if details is not None else None
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
    reasoning,
    params: Mapping[str, Any],
    response_format: ResponseFormat | None,
) -> Iterator[StreamEvent]:
    client = _client(provider)
    response_format_kwargs = {}
    schema = _response_format(response_format)
    if schema is not None:
        response_format_kwargs["response_format"] = schema

    response = client.chat.completions.create(
        model=model_id_for(native_model_id, reasoning),
        messages=to_wire(messages, system),
        tools=_tools(tools),
        stream=True,
        **_generation_kwargs(params),
        **response_format_kwargs,
        # Without this an OpenAI-shaped stream never sends usage at all — it
        # is not discarded, it is never requested.
        stream_options={"include_usage": True},
    )

    text = ""
    yielded = 0
    holding = False
    hold_decided = False
    calls: dict[int, dict[str, str]] = {}
    usage: Usage | None = None

    def release() -> Iterator[StreamEvent]:
        nonlocal yielded
        pending = text[yielded:]
        yielded = len(text)
        if pending:
            yield TextDelta(pending)

    for chunk in response:
        usage = _usage(getattr(chunk, "usage", None)) or usage
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            continue
        delta = choices[0].delta
        if delta is None:
            continue

        if getattr(delta, "content", None):
            text += delta.content
            if not hold_decided:
                if text.lstrip().startswith("{"):
                    holding = True
                elif text.strip():
                    hold_decided = True
            if holding and len(text) - yielded >= ERROR_PAYLOAD_HOLD_CHARS:
                holding, hold_decided = False, True
            if not holding:
                yield from release()

        for tc in getattr(delta, "tool_calls", None) or []:
            if holding:
                holding, hold_decided = False, True
                yield from release()
            slot = calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
            if tc.id:
                slot["id"] = tc.id
            if tc.function and tc.function.name:
                slot["name"] += tc.function.name
            if tc.function and tc.function.arguments:
                slot["args"] += tc.function.arguments

    if holding:
        message = _error_payload_message(text)
        if message:
            raise AdapterError(message)
        yield from release()

    if calls:
        parsed = []
        for slot in calls.values():
            try:
                args = json.loads(slot["args"] or "{}")
            except ValueError:
                args = {}
            parsed.append(ToolCall(slot["id"], slot["name"], args if isinstance(args, dict) else {}))
        yield Completed(text=text, tool_calls=tuple(parsed), finish_reason="tool_calls", usage=usage)
        return

    if not text:
        raise AdapterError("The model returned an empty response — try again later.")
    yield Completed(text=text, finish_reason="stop", usage=usage)


def test_connection(provider: Any, native_model_id: str) -> dict[str, Any]:
    try:
        client = _client(provider)
        response = client.chat.completions.create(
            model=native_model_id,
            messages=[{"role": "user", "content": 'Say "ready" and nothing else.'}],
        )
        text = response.choices[0].message.content if response.choices else None
        if not text:
            return {"ok": False, "error": "The server responded but with no text — check the model name."}
        return {"ok": True, "error": None}
    except NoCredential:
        return {"ok": False, "error": "That server needs an API key."}
    except Exception as err:  # noqa: BLE001
        status = getattr(err, "status_code", None) or getattr(err, "status", None)
        from ..credentials import resolve
        if status == 401 and not resolve(provider.credential_ref):
            return {"ok": False, "error": "That server was reached but needs an API key."}
        return {"ok": False, "error": friendly_error(err)}


def discover_models(provider: Any) -> list[dict[str, Any]]:
    """What this server says it has. The standard listing carries almost
    nothing — an id and some timestamps — but servers speaking this format
    routinely add to it, and the additions answer questions no first-party
    API answers at all: `context_length` (an aggregator's own field, not
    OpenAI's) and `supported_parameters` (whether this model can be asked to
    reason, without spending a rejected request to find out)."""
    client = _client(provider)
    out = []
    for model in client.models.list():
        row: dict[str, Any] = {"model": model.id,
                               "contextTokens": getattr(model, "context_length", None)}
        for extra in ("supported_parameters", "capabilities"):
            value = getattr(model, extra, None)
            if value:
                row[extra] = value
        out.append(row)
    return out


def friendly_error(err: BaseException) -> str:
    """The human-readable half. SDK `.message` is raw JSON on every provider."""
    for attr_chain in (("body", "error", "message"), ("error", "message")):
        node: Any = err
        for attr in attr_chain:
            node = node.get(attr) if isinstance(node, dict) else getattr(node, attr, None)
            if node is None:
                break
        if isinstance(node, str) and node:
            return node
    message = str(err)
    if "Connection error" in message or "connect" in message.lower():
        return "Couldn't reach that address — is the server running?"
    return message or "That connection didn't work."
