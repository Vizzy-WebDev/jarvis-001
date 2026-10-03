"""OpenAI-compatible Chat Completions — what OpenRouter, vLLM, LM Studio, Ollama's
OpenAI endpoint and LiteLLM proxies speak.

The servers differ, and those differences are data: a quirk profile's `wire`
flags, read here and nowhere else.

    no_parallel_tool_calls   don't send parallel_tool_calls (the server rejects it)
    tool_args_not_streamed   tool arguments arrive whole at the end, not in pieces
    rejects_strict           never send a strict JSON schema (use JSON mode)
    no_stream_usage          don't ask for usage in the stream (the server rejects it)
    no_system_role           no "system" messages: instructions open the first user turn
    no_tool_call_ids         the server gives no tool-call ids (one is made for it)
    error_envelope_in_text   the server may answer 200 and stream its error as the text
    reasoning_param          how effort is sent: "reasoning_effort" | "reasoning_object"
    max_tokens_param         "max_tokens" (default) | "max_completion_tokens"
    show_endpoint            discovery also asks Ollama's native /api/show per model
    catalog_fields           "rich": the model list carries capabilities and prices
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterator

from .. import errors
from ..catalog import Pricing
from ..prepared import ConnInfo, Discovered, Prepared, Unexpressible
from ..types import (Finish, ImagePart, Message, ReasoningDelta, Sealed, SealedEvent, TextDelta, TextPart,
                     ToolArgsDelta, ToolCall, ToolCallCompleted, ToolCallStarted, ToolResult, Usage)
from . import _turns, _wire

NAME = "openai_chat"
QUIRKS = frozenset({"no_parallel_tool_calls", "tool_args_not_streamed", "rejects_strict", "no_stream_usage",
                    "no_system_role", "no_tool_call_ids", "error_envelope_in_text", "reasoning_param",
                    "max_tokens_param", "show_endpoint", "catalog_fields"})
DEFAULT_CAPABILITIES: dict[str, Any] = {"text_in": True, "tools": True, "parallel_tools": True, "streaming": True}

_FINISH = {"stop": "stop", "tool_calls": "tool_calls", "function_call": "tool_calls", "length": "length",
           "content_filter": "content_filter"}


def _auth(conn: ConnInfo) -> dict[str, str]:
    return {"Authorization": f"Bearer {conn.api_key}"} if conn.api_key else {}


# --- schemas ---------------------------------------------------------------------------------

def translate_schema(schema: Any, quirks: Any = None) -> Any:
    """Plain JSON Schema is this format's own language. The one rewrite: local `$ref`s
    are written out in place (the same schema) — found live, a compatible server
    refused the whole request over a connector tool's `$ref`. A schema that isn't an
    object at the top can't be a function's parameters."""
    if not isinstance(schema, dict):
        raise Unexpressible("a tool's parameters must be a JSON object schema")
    return _wire.inline_refs(schema)


# --- the request -----------------------------------------------------------------------------

def _user_content(message: Message) -> Any:
    if all(isinstance(p, TextPart) for p in message.parts):
        return message.text
    parts: list[dict[str, Any]] = []
    for part in message.parts:
        if isinstance(part, TextPart):
            parts.append({"type": "text", "text": part.text})
        elif isinstance(part, ImagePart):
            parts.append({"type": "image_url", "image_url": {"url": f"data:{part.mime};base64,{part.data_b64}"}})
    return parts


def messages(prepared: Prepared, quirks: dict[str, Any]) -> list[dict[str, Any]]:
    ids = _turns.native_ids(prepared.items)
    out: list[dict[str, Any]] = []
    system = _turns.system_text(prepared)
    pending_system = system if quirks.get("no_system_role") else ""
    if system and not pending_system:
        out.append({"role": "system", "content": system})
    for turn in _turns.turns(prepared.items):
        if turn.role == "user":
            message = turn.items[0]
            content = _user_content(message)  # type: ignore[arg-type]
            if pending_system:
                content = (f"{pending_system}\n\n{content}" if isinstance(content, str)
                           else [{"type": "text", "text": pending_system}, *content])
                pending_system = ""
            out.append({"role": "user", "content": content})
        elif turn.role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": _turns.text_of(turn) or None}
            calls = _turns.tool_calls(turn)
            if calls:
                entry["tool_calls"] = [{"id": ids.get(c.id, c.id), "type": "function", "function": {
                    "name": c.name,
                    "arguments": c.raw_arguments if c.arguments is None else _wire.dumps(dict(c.arguments))}}
                    for c in calls]
            for item in turn.items:
                if isinstance(item, Sealed) and item.kind == "reasoning_details":
                    entry["reasoning_details"] = item.payload
            if entry["content"] is None and "tool_calls" not in entry:
                continue
            out.append(entry)
        else:
            for result in turn.items:
                assert isinstance(result, ToolResult)
                out.append({"role": "tool", "tool_call_id": ids.get(result.call_id, result.call_id),
                            "content": _wire.tool_output(result.content)})
    if pending_system:  # nothing from the user yet
        out.insert(0, {"role": "user", "content": pending_system})
    return out


def _effort(prepared: Prepared, quirks: dict[str, Any], body: dict[str, Any]) -> None:
    if prepared.effort is None:
        return
    how = quirks.get("reasoning_param") or "reasoning_effort"
    if how == "reasoning_object":
        body["reasoning"] = {"enabled": False} if prepared.effort == "none" else {"effort": prepared.effort}
    else:
        body["reasoning_effort"] = prepared.effort


def body_for(prepared: Prepared, quirks: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {"model": prepared.model_id, "messages": messages(prepared, quirks), "stream": True}
    if not quirks.get("no_stream_usage"):
        body["stream_options"] = {"include_usage": True}
    if prepared.tools:
        body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                            "parameters": dict(t.parameters)}}
                         for t in prepared.tools]
        if not quirks.get("no_parallel_tool_calls"):
            body["parallel_tool_calls"] = prepared.parallel_tools
    mode = prepared.output.mode
    if mode == "strict" and not quirks.get("rejects_strict"):
        body["response_format"] = {"type": "json_schema", "json_schema": {
            "name": prepared.output.name, "schema": dict(prepared.output.schema or {}), "strict": True}}
    elif mode in ("json_mode", "strict"):
        body["response_format"] = {"type": "json_object"}
    if prepared.max_output_tokens:
        body[quirks.get("max_tokens_param") or "max_tokens"] = prepared.max_output_tokens
    _effort(prepared, quirks, body)
    from ..prepared import deep_merge

    return deep_merge(body, prepared.params)


# --- the reply -------------------------------------------------------------------------------

def _usage(raw: dict[str, Any]) -> Usage:
    return Usage(input=_wire.count(raw.get("prompt_tokens")), output=_wire.count(raw.get("completion_tokens")),
                 cached=_wire.count((raw.get("prompt_tokens_details") or {}).get("cached_tokens")),
                 reasoning=_wire.count((raw.get("completion_tokens_details") or {}).get("reasoning_tokens")),
                 raw=raw)


_ERROR_START = re.compile(r'^\s*\{\s*"error"\s*:')


def _could_be_envelope(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    return bool(_ERROR_START.match(text)) or '{"error":'.startswith(compact)


def _envelope(text: str, url: str) -> errors.ModelError | None:
    """A whole reply that is exactly one `{"error": {"message": ...}}` object is the
    server's failure, not an answer. Anything else is an answer."""
    try:
        body = json.loads(text)
    except ValueError:
        return None
    err = body.get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict) or not str(err.get("message") or "").strip():
        return None
    return _wire.error_in_body(body, url)


def stream(conn: ConnInfo, prepared: Prepared) -> Iterator[Any]:
    quirks = dict(conn.quirks)
    url = _wire.join_url(conn.base_url, "chat/completions")
    body = body_for(prepared, quirks)
    hold = bool(quirks.get("error_envelope_in_text"))
    whole_args = bool(quirks.get("tool_args_not_streamed"))

    held: list[str] = []
    holding = hold
    slots: dict[int, dict[str, Any]] = {}
    finish: str | None = None
    done = False
    usage: Usage | None = None
    reported: str | None = None
    reasoning_details: list[Any] = []

    with _wire.post_stream(url, headers=_auth(conn), body=body) as response:
        for _, data in _wire.iter_sse(response):
            if data.strip() == "[DONE]":
                done = True
                break
            chunk = _wire.loads_event(data, url)
            if not isinstance(chunk, dict):
                continue
            if chunk.get("error"):
                raise _wire.error_in_body(chunk, url)
            reported = chunk.get("model") or reported
            if isinstance(chunk.get("usage"), dict):
                usage = _usage(chunk["usage"])
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("reasoning"):
                    yield ReasoningDelta(str(delta["reasoning"]))
                if isinstance(delta.get("reasoning_details"), list):
                    reasoning_details.extend(delta["reasoning_details"])
                piece = delta.get("content")
                if piece:
                    if holding:
                        held.append(piece)
                        if _could_be_envelope("".join(held)):
                            continue
                        holding = False
                        piece = "".join(held)
                    yield TextDelta(piece)
                for part in delta.get("tool_calls") or []:
                    index = part.get("index", len(slots))
                    slot = slots.get(index)
                    if slot is None:
                        slot = slots[index] = {"id": None, "native": None, "name": None, "args": []}
                    if part.get("id"):
                        slot["native"] = part["id"]
                    fn = part.get("function") or {}
                    if fn.get("name") and not slot["name"]:  # some servers repeat it on every chunk
                        slot["name"] = fn["name"]
                    if slot["name"] and slot["id"] is None:
                        slot["id"] = prepared.mint_id()
                        yield ToolCallStarted(slot["id"], slot["name"])
                        if slot["args"] and not whole_args:  # pieces that came before the name
                            yield ToolArgsDelta(slot["id"], "".join(slot["args"]))
                    if fn.get("arguments"):
                        slot["args"].append(fn["arguments"])
                        if slot["id"] is not None and not whole_args:
                            yield ToolArgsDelta(slot["id"], fn["arguments"])
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]

    if holding and held:
        failure = _envelope("".join(held), url)
        if failure is not None:
            raise failure
        yield TextDelta("".join(held))
    if finish is None and not done:
        raise errors.Unavailable(f"The reply from {_wire.host_of(url)} stopped part-way.")

    if reasoning_details:
        yield SealedEvent("reasoning_details", reasoning_details)
    mapping: dict[str, str] = {}
    for index in sorted(slots):
        slot = slots[index]
        if not slot["name"]:
            raise errors.Unavailable("The model asked to use a tool without naming it, so nothing was run.")
        raw = "".join(slot["args"])
        if whole_args and raw:
            yield ToolArgsDelta(slot["id"], raw)
        args, bad = _wire.parse_arguments(raw)
        if slot["native"] and slot["native"] != slot["id"]:
            mapping[slot["id"]] = slot["native"]
        yield ToolCallCompleted(ToolCall(slot["id"], slot["name"], args, bad))
    if mapping:
        yield SealedEvent("ids", mapping)
    stop = "tool_calls" if slots else _FINISH.get(finish or "stop", "stop")
    yield Finish(stop, usage or Usage(), reported)


# --- discovery -------------------------------------------------------------------------------

def _per_million(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number < 0 else number * 1_000_000


def _listed(row: dict[str, Any]) -> Discovered:
    """Only what the server reported about the model. A plain server says nothing
    beyond the id; a gateway may describe capabilities, modalities and price."""
    caps: dict[str, Any] = {}
    arch = row.get("architecture") if isinstance(row.get("architecture"), dict) else {}
    inputs = row.get("input_modalities") or arch.get("input_modalities")
    outputs = row.get("output_modalities") or arch.get("output_modalities")
    if isinstance(inputs, list) and inputs:
        caps["image_in"] = "image" in inputs
        caps["pdf_in"] = "file" in inputs
    kind = str(row.get("type") or "").lower()
    if kind in {"embedding", "embeddings"}:
        caps["embeddings"] = True
        caps["text_in"] = False
    elif kind in {"video", "image", "audio", "speech", "tts", "stt", "transcription", "rerank", "moderation"} or (
            isinstance(outputs, list) and outputs and "text" not in outputs):
        caps["text_in"] = False
    supported = row.get("supported_parameters")
    if isinstance(supported, list) and supported:
        caps["tools"] = "tools" in supported
        caps["structured_output_strict"] = "structured_outputs" in supported
        caps["json_mode"] = "response_format" in supported
        caps["reasoning_control"] = "reasoning" in supported or "reasoning_effort" in supported
    listed_caps = row.get("capabilities") if isinstance(row.get("capabilities"), dict) else {}
    if isinstance(listed_caps.get("tool_calling"), bool):
        caps["tools"] = listed_caps["tool_calling"]
    context = _wire.count(row.get("context_length"))
    if context:
        caps["max_context_tokens"] = context
    top = row.get("top_provider") if isinstance(row.get("top_provider"), dict) else {}
    ceiling = _wire.count(top.get("max_completion_tokens"))
    if ceiling:
        caps["max_output_tokens"] = ceiling
    pricing = None
    price = row.get("pricing") if isinstance(row.get("pricing"), dict) else {}
    prompt, completion = _per_million(price.get("prompt")), _per_million(price.get("completion"))
    if prompt is not None and completion is not None:
        pricing = Pricing(prompt, completion, _per_million(price.get("input_cache_read")))
    return Discovered(model_id=str(row["id"]), label=row.get("name") or None, capabilities=caps, pricing=pricing)


def _show(conn: ConnInfo, model_id: str) -> dict[str, Any]:
    """Ollama's native description of one model: what it can do and its context."""
    body = _wire.post_json(_wire.join_url(_wire.root_of(conn.base_url), "api/show"), headers=_auth(conn),
                           body={"model": model_id}, timeout=_wire.LIST_TIMEOUT_S)
    return body if isinstance(body, dict) else {}


def _from_show(found: Discovered, shown: dict[str, Any]) -> Discovered:
    caps = dict(found.capabilities)
    listed = shown.get("capabilities")
    if isinstance(listed, list):
        caps["text_in"] = "completion" in listed
        caps["tools"] = "tools" in listed
        caps["image_in"] = "vision" in listed
        caps["reasoning_control"] = "thinking" in listed
        caps["embeddings"] = "embedding" in listed
    info = shown.get("model_info") if isinstance(shown.get("model_info"), dict) else {}
    for key, value in info.items():
        if key.endswith(".context_length") and _wire.count(value):
            caps["max_context_tokens"] = value
            break
    details = shown.get("details") if isinstance(shown.get("details"), dict) else {}
    return Discovered(model_id=found.model_id, label=found.label, capabilities=caps, pricing=found.pricing,
                      family=details.get("family") or found.family, upstream=found.upstream)


def discover(conn: ConnInfo) -> list[Discovered]:
    body = _wire.get_json(_wire.join_url(conn.base_url, "models"), headers=_auth(conn),
                          not_found="Reached the server. It doesn't offer a list of its models, so add the model "
                                    "ID by hand.")
    rows = body.get("data") if isinstance(body, dict) else body
    if not isinstance(rows, list):
        raise errors.InvalidRequest(f"{_wire.host_of(conn.base_url)} answered, but not with a list of models.")
    found = [_listed(r) for r in rows if isinstance(r, dict) and r.get("id")]
    if conn.quirks.get("show_endpoint"):
        described = []
        for item in found:
            try:
                described.append(_from_show(item, _show(conn, item.model_id)))
            except errors.ModelError:
                described.append(item)  # one model it won't describe doesn't lose the rest
        found = described
    return found


# --- embeddings ------------------------------------------------------------------------------

def embed(conn: ConnInfo, model_id: str, inputs: list[str]) -> list[list[float]]:
    url = _wire.join_url(conn.base_url, "embeddings")
    body = _wire.post_json(url, headers=_auth(conn), body={"model": model_id, "input": inputs})
    rows = body.get("data") if isinstance(body, dict) else None
    if isinstance(body, dict) and body.get("error"):
        raise _wire.error_in_body(body, url)
    if not isinstance(rows, list) or len(rows) != len(inputs):
        raise errors.Unavailable(f"{_wire.host_of(url)} didn't return one vector per input.")
    ordered = sorted(rows, key=lambda r: r.get("index", 0) if isinstance(r, dict) else 0)
    vectors = []
    for row in ordered:
        vector = row.get("embedding") if isinstance(row, dict) else None
        if not isinstance(vector, list):
            raise errors.Unavailable(f"{_wire.host_of(url)} sent an embedding Jarvis couldn't read.")
        vectors.append([float(v) for v in vector])
    return vectors
