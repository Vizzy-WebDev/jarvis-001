"""The Responses API (OpenAI's, and Open Responses servers), used statelessly.

* Always `store: false`: the caller owns the conversation and resends it.
* Encrypted reasoning is requested (`include: ["reasoning.encrypted_content"]`)
  and each reasoning item is carried verbatim as a Sealed "reasoning" item, sent
  back only to the endpoint that made it.
* Effort is `reasoning.effort`; a strict schema is `text.format`.

Quirk flags:
    no_encrypted_reasoning   the server refuses the `include` for encrypted reasoning
"""

from __future__ import annotations

from typing import Any, Iterator

from .. import errors
from ..prepared import ConnInfo, Discovered, Prepared, Unexpressible, deep_merge
from ..types import (Finish, ImagePart, Message, ReasoningDelta, Sealed, SealedEvent, TextDelta, TextPart,
                     ToolArgsDelta, ToolCall, ToolCallCompleted, ToolCallStarted, ToolResult, Usage)
from . import _turns, _wire

NAME = "openai_responses"
QUIRKS = frozenset({"no_encrypted_reasoning"})
DEFAULT_CAPABILITIES: dict[str, Any] = {"text_in": True, "tools": True, "parallel_tools": True, "streaming": True,
                                        "json_mode": True}

_INCOMPLETE = {"max_output_tokens": "length", "content_filter": "content_filter"}


def _auth(conn: ConnInfo) -> dict[str, str]:
    return {"Authorization": f"Bearer {conn.api_key}"} if conn.api_key else {}


def translate_schema(schema: Any, quirks: Any = None) -> Any:
    if not isinstance(schema, dict):
        raise Unexpressible("a tool's parameters must be a JSON object schema")
    return schema


# --- the request -----------------------------------------------------------------------------

def _user(message: Message) -> dict[str, Any]:
    if all(isinstance(p, TextPart) for p in message.parts):
        return {"role": "user", "content": message.text}
    parts: list[dict[str, Any]] = []
    for part in message.parts:
        if isinstance(part, TextPart):
            parts.append({"type": "input_text", "text": part.text})
        elif isinstance(part, ImagePart):
            parts.append({"type": "input_image", "image_url": f"data:{part.mime};base64,{part.data_b64}"})
    return {"role": "user", "content": parts}


def input_items(prepared: Prepared) -> list[dict[str, Any]]:
    ids = _turns.native_ids(prepared.items)
    out: list[dict[str, Any]] = []
    for turn in _turns.turns(prepared.items):
        if turn.role == "user":
            out.append(_user(turn.items[0]))  # type: ignore[arg-type]
            continue
        for item in turn.items:
            if isinstance(item, Sealed) and item.kind == "reasoning" and isinstance(item.payload, dict):
                out.append(dict(item.payload))
            elif isinstance(item, Message) and item.text:
                out.append({"role": "assistant", "content": item.text})
            elif isinstance(item, ToolCall):
                native = ids.get(item.id) or {}
                entry: dict[str, Any] = {"type": "function_call", "call_id": native.get("call_id", item.id),
                                         "name": item.name,
                                         "arguments": item.raw_arguments if item.arguments is None
                                         else _wire.dumps(dict(item.arguments))}
                if native.get("id"):
                    entry["id"] = native["id"]
                out.append(entry)
            elif isinstance(item, ToolResult):
                native = ids.get(item.call_id) or {}
                out.append({"type": "function_call_output", "call_id": native.get("call_id", item.call_id),
                            "output": _wire.tool_output(item.content)})
    return out


def body_for(prepared: Prepared, quirks: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {"model": prepared.model_id, "input": input_items(prepared), "stream": True,
                            "store": False}
    if not quirks.get("no_encrypted_reasoning"):
        body["include"] = ["reasoning.encrypted_content"]
    instructions = _turns.system_text(prepared)
    if instructions:
        body["instructions"] = instructions
    if prepared.tools:
        body["tools"] = [{"type": "function", "name": t.name, "description": t.description,
                          "parameters": dict(t.parameters), "strict": False} for t in prepared.tools]
        body["parallel_tool_calls"] = prepared.parallel_tools
    if prepared.output.mode == "strict":
        body["text"] = {"format": {"type": "json_schema", "name": prepared.output.name,
                                   "schema": dict(prepared.output.schema or {}), "strict": True}}
    elif prepared.output.mode == "json_mode":
        body["text"] = {"format": {"type": "json_object"}}
    if prepared.effort:
        body["reasoning"] = {"effort": prepared.effort}
    if prepared.max_output_tokens:
        body["max_output_tokens"] = prepared.max_output_tokens
    return deep_merge(body, prepared.params)


# --- the reply -------------------------------------------------------------------------------

def _usage(raw: dict[str, Any]) -> Usage:
    return Usage(input=_wire.count(raw.get("input_tokens")), output=_wire.count(raw.get("output_tokens")),
                 cached=_wire.count((raw.get("input_tokens_details") or {}).get("cached_tokens")),
                 reasoning=_wire.count((raw.get("output_tokens_details") or {}).get("reasoning_tokens")), raw=raw)


def stream(conn: ConnInfo, prepared: Prepared) -> Iterator[Any]:
    url = _wire.join_url(conn.base_url, "responses")
    calls: dict[str, dict[str, Any]] = {}  # by the server's item id
    mapping: dict[str, dict[str, str]] = {}
    refused = False
    final: dict[str, Any] | None = None

    with _wire.post_stream(url, headers=_auth(conn), body=body_for(prepared, dict(conn.quirks))) as response:
        for event, data in _wire.iter_sse(response):
            payload = _wire.loads_event(data, url)
            if not isinstance(payload, dict):
                continue
            kind = payload.get("type") or event
            if kind == "response.output_text.delta" and payload.get("delta"):
                yield TextDelta(payload["delta"])
            elif kind == "response.refusal.delta" and payload.get("delta"):
                refused = True
                yield TextDelta(payload["delta"])
            elif kind in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta") \
                    and payload.get("delta"):
                yield ReasoningDelta(payload["delta"])
            elif kind == "response.output_item.added":
                item = payload.get("item") or {}
                if item.get("type") == "function_call":
                    cid = prepared.mint_id()
                    calls[item.get("id") or cid] = {"id": cid, "name": item.get("name") or "", "args": []}
                    yield ToolCallStarted(cid, item.get("name") or "")
            elif kind == "response.function_call_arguments.delta":
                slot = calls.get(payload.get("item_id") or "")
                if slot is not None and payload.get("delta"):
                    slot["args"].append(payload["delta"])
                    yield ToolArgsDelta(slot["id"], payload["delta"])
            elif kind == "response.output_item.done":
                item = payload.get("item") or {}
                if item.get("type") == "reasoning":
                    yield SealedEvent("reasoning", dict(item))
                elif item.get("type") == "function_call":
                    slot = calls.get(item.get("id") or "")
                    if slot is None:  # announced only on completion
                        slot = {"id": prepared.mint_id(), "name": item.get("name") or "", "args": []}
                        yield ToolCallStarted(slot["id"], slot["name"])
                    raw = item.get("arguments")
                    raw = raw if isinstance(raw, str) else "".join(slot["args"])
                    parsed, bad = _wire.parse_arguments(raw)
                    mapping[slot["id"]] = {k: v for k, v in (("call_id", item.get("call_id")),
                                                            ("id", item.get("id"))) if v}
                    yield ToolCallCompleted(ToolCall(slot["id"], item.get("name") or slot["name"], parsed, bad))
            elif kind in ("response.completed", "response.incomplete"):
                final = payload.get("response") or {}
                break
            elif kind == "response.failed":
                failed = (payload.get("response") or {}).get("error") or {}
                raise _wire.error_in_body({"error": failed}, url)
            elif kind == "error":
                raise _wire.error_in_body({"error": payload.get("error") or payload}, url)

    if final is None:
        raise errors.Unavailable(f"The reply from {_wire.host_of(url)} stopped part-way.")
    if mapping:
        yield SealedEvent("ids", mapping)
    if final.get("status") == "incomplete":
        stop = _INCOMPLETE.get((final.get("incomplete_details") or {}).get("reason") or "", "length")
    elif refused:
        stop = "content_filter"
    else:
        stop = "tool_calls" if mapping else "stop"
    usage = _usage(final["usage"]) if isinstance(final.get("usage"), dict) else Usage()
    yield Finish(stop, usage, final.get("model"))


# --- discovery -------------------------------------------------------------------------------

def discover(conn: ConnInfo) -> list[Discovered]:
    """The list says nothing about what each model can do — only its id."""
    body = _wire.get_json(_wire.join_url(conn.base_url, "models"), headers=_auth(conn))
    rows = body.get("data") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        raise errors.InvalidRequest(f"{_wire.host_of(conn.base_url)} answered, but not with a list of models.")
    return [Discovered(model_id=str(r["id"])) for r in rows if isinstance(r, dict) and r.get("id")]
