"""Anthropic's Messages API.

* Thinking blocks (and redacted ones) come back with a signature and must be
  handed back exactly as received — empty text included — when the conversation
  continues on the same endpoint. Each is carried as a Sealed "thinking" item in
  the position it arrived and replayed verbatim; any other endpoint never sees it.
* The stable prefix gets a `cache_control` breakpoint on its last system block.
* Effort is `output_config.effort`; a strict schema is `output_config.format`.
  Current models can't turn thinking off, so "none" isn't among the levels this
  declares: the adapter sends the nearest accepted level and reports it.
* `max_tokens` is required: the hint, else the model's reported ceiling (capped),
  else a size every current model accepts.
"""

from __future__ import annotations

from typing import Any, Iterator

from .. import errors
from ..prepared import ConnInfo, Discovered, Prepared, Unexpressible, deep_merge
from ..types import (Finish, ImagePart, Message, ReasoningDelta, Sealed, SealedEvent, TextDelta, TextPart,
                     ToolArgsDelta, ToolCall, ToolCallCompleted, ToolCallStarted, ToolResult, Usage)
from . import _turns, _wire

NAME = "anthropic_messages"
QUIRKS: frozenset[str] = frozenset()
#: What the protocol takes; the model list's own `capabilities` override it per model.
DEFAULT_CAPABILITIES: dict[str, Any] = {"text_in": True, "tools": True, "parallel_tools": True,
                                        "streaming": True, "prompt_caching": True, "image_in": True,
                                        # The API's own default is "high" (the same as sending none).
                                        "effort_levels": ["low", "medium", "high"], "effort_default": "high"}
VERSION = "2023-06-01"
PREFERRED_MAX_TOKENS = 64_000
FALLBACK_MAX_TOKENS = 16_384

_STOP = {"end_turn": "stop", "stop_sequence": "stop", "pause_turn": "stop", "tool_use": "tool_calls",
         "max_tokens": "length", "model_context_window_exceeded": "length", "refusal": "content_filter"}


def _auth(conn: ConnInfo) -> dict[str, str]:
    headers = {"anthropic-version": VERSION}
    if conn.api_key:
        headers["x-api-key"] = conn.api_key
    return headers


def translate_schema(schema: Any, quirks: Any = None) -> Any:
    if not isinstance(schema, dict):
        raise Unexpressible("a tool's input schema must be a JSON object schema")
    return schema


# --- the request -----------------------------------------------------------------------------

def _system(prepared: Prepared) -> list[dict[str, Any]]:
    stable = "\n\n".join(s.text for s in prepared.system if s.stable)
    rest = "\n\n".join(s.text for s in prepared.system if not s.stable)
    blocks: list[dict[str, Any]] = []
    if stable:
        block: dict[str, Any] = {"type": "text", "text": stable}
        if prepared.cache:
            block["cache_control"] = {"type": "ephemeral"}
        blocks.append(block)
    if rest:
        blocks.append({"type": "text", "text": rest})
    return blocks


def _user(message: Message) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for part in message.parts:
        if isinstance(part, TextPart):
            if part.text:
                blocks.append({"type": "text", "text": part.text})
        elif isinstance(part, ImagePart):
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": part.mime,
                                                       "data": part.data_b64}})
    return blocks


def messages(prepared: Prepared) -> list[dict[str, Any]]:
    ids = _turns.native_ids(prepared.items)
    out: list[dict[str, Any]] = []

    def put(role: str, blocks: list[dict[str, Any]]) -> None:
        if not blocks:
            return
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)  # the API wants the sides to alternate
        else:
            out.append({"role": role, "content": blocks})

    for turn in _turns.turns(prepared.items):
        if turn.role == "user":
            put("user", _user(turn.items[0]))  # type: ignore[arg-type]
        elif turn.role == "assistant":
            blocks: list[dict[str, Any]] = []
            for item in turn.items:
                if isinstance(item, Sealed) and item.kind == "thinking" and isinstance(item.payload, dict):
                    blocks.append(dict(item.payload))
                elif isinstance(item, Message) and item.text:
                    blocks.append({"type": "text", "text": item.text})
                elif isinstance(item, ToolCall):
                    blocks.append({"type": "tool_use", "id": ids.get(item.id, item.id), "name": item.name,
                                   "input": dict(item.arguments) if item.arguments is not None else {}})
            put("assistant", blocks)
        else:
            put("user", [{"type": "tool_result", "tool_use_id": ids.get(r.call_id, r.call_id),
                          "content": _wire.tool_output(r.content), **({"is_error": True} if r.is_error else {})}
                         for r in turn.items if isinstance(r, ToolResult)])
    return out


def body_for(prepared: Prepared) -> dict[str, Any]:
    ceiling = _wire.count(prepared.caps.get("max_output_tokens"))
    body: dict[str, Any] = {
        "model": prepared.model_id,
        "max_tokens": prepared.max_output_tokens or (min(ceiling, PREFERRED_MAX_TOKENS) if ceiling
                                                     else FALLBACK_MAX_TOKENS),
        "messages": messages(prepared),
        "stream": True,
    }
    system = _system(prepared)
    if system:
        body["system"] = system
    if prepared.tools:
        body["tools"] = [{"name": t.name, "description": t.description, "input_schema": dict(t.parameters)}
                         for t in prepared.tools]
        if not prepared.parallel_tools:
            body["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}
    output_config: dict[str, Any] = {}
    if prepared.effort:
        output_config["effort"] = prepared.effort
    if prepared.output.mode == "strict":
        output_config["format"] = {"type": "json_schema", "schema": dict(prepared.output.schema or {})}
    if output_config:
        body["output_config"] = output_config
    return deep_merge(body, prepared.params)


# --- the reply -------------------------------------------------------------------------------

def stream(conn: ConnInfo, prepared: Prepared) -> Iterator[Any]:
    url = _wire.join_url(conn.base_url, "messages")
    blocks: dict[int, dict[str, Any]] = {}
    args: dict[int, list[str]] = {}
    canonical: dict[int, str] = {}
    mapping: dict[str, str] = {}
    usage: dict[str, Any] = {}
    reported: str | None = None
    stop_reason: str | None = None
    finished = False

    with _wire.post_stream(url, headers=_auth(conn), body=body_for(prepared)) as response:
        for event, data in _wire.iter_sse(response):
            payload = _wire.loads_event(data, url)
            if not isinstance(payload, dict):
                continue
            kind = payload.get("type") or event
            if kind == "message_start":
                message = payload.get("message") or {}
                reported = message.get("model") or reported
                usage.update(message.get("usage") or {})
            elif kind == "content_block_start":
                index = payload["index"]
                block = dict(payload.get("content_block") or {})
                blocks[index] = block
                if block.get("type") == "tool_use":
                    args[index] = []
                    canonical[index] = prepared.mint_id()
                    mapping[canonical[index]] = block.get("id") or canonical[index]
                    yield ToolCallStarted(canonical[index], block.get("name") or "")
            elif kind == "content_block_delta":
                index, delta = payload["index"], payload.get("delta") or {}
                block = blocks.setdefault(index, {})
                dtype = delta.get("type")
                if dtype == "text_delta" and delta.get("text"):
                    block["text"] = block.get("text", "") + delta["text"]
                    yield TextDelta(delta["text"])
                elif dtype == "input_json_delta":
                    piece = delta.get("partial_json") or ""
                    args.setdefault(index, []).append(piece)
                    if piece and index in canonical:
                        yield ToolArgsDelta(canonical[index], piece)
                elif dtype == "thinking_delta":
                    block["thinking"] = block.get("thinking", "") + (delta.get("thinking") or "")
                    if delta.get("thinking"):
                        yield ReasoningDelta(delta["thinking"])
                elif dtype == "signature_delta":
                    block["signature"] = block.get("signature", "") + (delta.get("signature") or "")
            elif kind == "content_block_stop":
                index = payload["index"]
                block = blocks.get(index) or {}
                if block.get("type") in ("thinking", "redacted_thinking"):
                    yield SealedEvent("thinking", dict(block))
                elif block.get("type") == "tool_use":
                    parsed, bad = _wire.parse_arguments("".join(args.get(index) or []))
                    yield ToolCallCompleted(ToolCall(canonical[index], block.get("name") or "", parsed, bad))
            elif kind == "message_delta":
                stop_reason = (payload.get("delta") or {}).get("stop_reason") or stop_reason
                usage.update({k: v for k, v in (payload.get("usage") or {}).items() if v is not None})
            elif kind == "message_stop":
                finished = True
                break
            elif kind == "error":
                raise _wire.error_in_body(payload, url)

    if not finished:
        raise errors.Unavailable(f"The reply from {_wire.host_of(url)} stopped part-way.")
    native = {c: n for c, n in mapping.items() if n != c}
    if native:
        yield SealedEvent("ids", native)
    reading = _wire.count(usage.get("cache_read_input_tokens"))
    creating = _wire.count(usage.get("cache_creation_input_tokens")) or 0
    fresh = _wire.count(usage.get("input_tokens"))
    yield Finish(
        "tool_calls" if canonical and stop_reason == "tool_use" else _STOP.get(stop_reason or "end_turn", "stop"),
        Usage(input=None if fresh is None else fresh + creating + (reading or 0),
              output=_wire.count(usage.get("output_tokens")), cached=reading,
              reasoning=_wire.count((usage.get("output_tokens_details") or {}).get("thinking_tokens")),
              raw=usage),
        reported)


# --- discovery -------------------------------------------------------------------------------

def _supported(caps: dict[str, Any], *path: str) -> bool | None:
    node: Any = caps
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return bool(node.get("supported")) if isinstance(node, dict) else None


def _listed(model: dict[str, Any]) -> Discovered:
    caps: dict[str, Any] = {}
    context = _wire.count(model.get("max_input_tokens"))
    if context:
        caps["max_context_tokens"] = context
    ceiling = _wire.count(model.get("max_tokens"))
    if ceiling:
        caps["max_output_tokens"] = ceiling
    reported = model.get("capabilities") if isinstance(model.get("capabilities"), dict) else {}
    for name, path in (("image_in", ("image_input",)), ("pdf_in", ("pdf_input",)),
                       ("structured_output_strict", ("structured_outputs",)), ("reasoning_control", ("effort",))):
        value = _supported(reported, *path)
        if value is not None:
            caps[name] = value
    # The list says, per model, which effort levels it takes; only canonical ones are kept.
    levels = [level for level in ("none", "low", "medium", "high") if _supported(reported, "effort", level)]
    if caps.get("reasoning_control") and levels:
        caps["effort_levels"] = levels
        if "high" in levels:
            caps["effort_default"] = "high"
    return Discovered(model_id=str(model["id"]), label=model.get("display_name") or None, capabilities=caps,
                      family="claude")


def discover(conn: ConnInfo) -> list[Discovered]:
    found: list[Discovered] = []
    after: str | None = None
    for _ in range(20):  # a bound, so a server that never says "no more" can't loop forever
        params: dict[str, Any] = {"limit": 1000}
        if after:
            params["after_id"] = after
        body = _wire.get_json(_wire.join_url(conn.base_url, "models"), headers=_auth(conn), params=params)
        rows = body.get("data") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            raise errors.InvalidRequest(f"{_wire.host_of(conn.base_url)} answered, but not with a list of models.")
        found.extend(_listed(m) for m in rows if isinstance(m, dict) and m.get("id"))
        after = body.get("last_id") if body.get("has_more") else None
        if not after:
            break
    return found
