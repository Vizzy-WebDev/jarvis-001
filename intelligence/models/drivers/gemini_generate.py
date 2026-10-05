"""Google Gemini's `generateContent`, streamed.

* A reply's parts may carry a `thoughtSignature` that must come back on the same
  part next time. Each is carried as a Sealed "thought_signature" item placed just
  before the item it belongs to, and reattached to that item on replay — only to
  the endpoint that made it.
* Schemas go as JSON Schema (`parametersJsonSchema`, `responseJsonSchema`). The one
  rewrite is lossless: an array that leaves its elements unsaid gets `items: {}`,
  which means exactly the same, because Google refuses an array with no `items` —
  and refuses the whole request with it. A schema that isn't an object at the top
  can't be expressed, and makes the endpoint ineligible.
* Image input only: no other attachment kind is sent.
* The key travels in the `x-goog-api-key` header, never in a URL.

Quirk flags:
    unsigned_call_signature  a function call from another endpoint has no signature;
                             newer models refuse one in the current turn unless it
                             carries this documented placeholder
    thinking_param           "level" (thinkingLevel, default) | "budget" (thinkingBudget)
"""

from __future__ import annotations

from typing import Any, Iterator
from urllib.parse import quote

from .. import errors
from ..prepared import ConnInfo, Discovered, Prepared, Unexpressible, deep_merge
from ..types import (Finish, ImagePart, Message, ReasoningDelta, Sealed, SealedEvent, TextDelta, TextPart,
                     ToolArgsDelta, ToolCall, ToolCallCompleted, ToolCallStarted, ToolResult, Usage)
from . import _turns, _wire

NAME = "gemini_generate"
QUIRKS = frozenset({"unsigned_call_signature", "thinking_param"})
DEFAULT_CAPABILITIES: dict[str, Any] = {"text_in": True, "tools": True, "parallel_tools": True, "streaming": True,
                                        "image_in": True, "json_mode": True, "structured_output_strict": True,
                                        # A thinking level can't be "off" ("minimal" still thinks).
                                        "effort_levels": ["low", "medium", "high"]}

_FINISH = {"STOP": "stop", "MAX_TOKENS": "length"}
_BLOCKED = {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY", "LANGUAGE"}
_LEVEL = {"none": "minimal", "low": "low", "medium": "medium", "high": "high"}
_BUDGET = {"none": 0, "low": 1024, "medium": 8192, "high": 24576}


def _auth(conn: ConnInfo) -> dict[str, str]:
    return {"x-goog-api-key": conn.api_key} if conn.api_key else {}


# --- schemas ---------------------------------------------------------------------------------

def _open_items(node: Any) -> Any:
    """`items: {}` wherever an array leaves it unsaid. In JSON Schema the two mean
    exactly the same thing (any element), so nothing is changed in meaning — only
    spelled the way Google requires: it refuses an array with no `items` at all,
    and with it the whole request."""
    if isinstance(node, list):
        return [_open_items(v) for v in node]
    if not isinstance(node, dict):
        return node
    out = {k: _open_items(v) for k, v in node.items()}
    if out.get("type") == "array" and "items" not in out and "prefixItems" not in out:
        out["items"] = {}
    return out


def translate_schema(schema: Any, quirks: Any = None) -> Any:
    if not isinstance(schema, dict):
        raise Unexpressible("a function's parameters must be a JSON object schema")
    return _open_items(_wire.inline_refs(schema))


# --- the request -----------------------------------------------------------------------------

def _user_parts(message: Message) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for part in message.parts:
        if isinstance(part, TextPart):
            if part.text:
                parts.append({"text": part.text})
        elif isinstance(part, ImagePart):
            parts.append({"inlineData": {"mimeType": part.mime, "data": part.data_b64}})
    return parts


def contents(prepared: Prepared, quirks: dict[str, Any]) -> list[dict[str, Any]]:
    ids = _turns.native_ids(prepared.items)
    placeholder = quirks.get("unsigned_call_signature")
    out: list[dict[str, Any]] = []

    def put(role: str, parts: list[dict[str, Any]]) -> None:
        if not parts:
            return
        if out and out[-1]["role"] == role:
            out[-1]["parts"].extend(parts)
        else:
            out.append({"role": role, "parts": parts})

    for turn in _turns.turns(prepared.items):
        if turn.role == "user":
            put("user", _user_parts(turn.items[0]))  # type: ignore[arg-type]
        elif turn.role == "assistant":
            parts: list[dict[str, Any]] = []
            signature: str | None = None
            signed_any = False
            for item in turn.items:
                if isinstance(item, Sealed) and item.kind == "thought_signature":
                    signature = (item.payload or {}).get("signature")
                    continue
                part: dict[str, Any] | None = None
                if isinstance(item, Message) and item.text:
                    part = {"text": item.text}
                elif isinstance(item, ToolCall):
                    call = {"name": item.name, "args": dict(item.arguments) if item.arguments is not None else {},
                            "id": ids.get(item.id, item.id)}
                    part = {"functionCall": call}
                    if signature is None and not signed_any and placeholder:
                        signature = placeholder  # first call of a turn made elsewhere
                if part is None:
                    continue
                if signature:
                    part["thoughtSignature"] = signature
                    signed_any = True
                    signature = None
                parts.append(part)
            put("model", parts)
        else:
            put("user", [{"functionResponse": {
                "name": r.name, "id": ids.get(r.call_id, r.call_id),
                "response": r.content if isinstance(r.content, dict) else {"result": r.content}}}
                for r in turn.items if isinstance(r, ToolResult)])
    return out


def body_for(prepared: Prepared, quirks: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {"contents": contents(prepared, quirks)}
    instruction = _turns.system_text(prepared)
    if instruction:
        body["systemInstruction"] = {"parts": [{"text": instruction}]}
    if prepared.tools:
        body["tools"] = [{"functionDeclarations": [{"name": t.name, "description": t.description,
                                                    "parametersJsonSchema": dict(t.parameters)}
                                                   for t in prepared.tools]}]
    config: dict[str, Any] = {}
    if prepared.output.mode == "strict":
        config.update(responseMimeType="application/json", responseJsonSchema=dict(prepared.output.schema or {}))
    elif prepared.output.mode == "json_mode":
        config["responseMimeType"] = "application/json"
    if prepared.max_output_tokens:
        config["maxOutputTokens"] = prepared.max_output_tokens
    if prepared.effort:
        if quirks.get("thinking_param") == "budget":
            config["thinkingConfig"] = {"thinkingBudget": _BUDGET[prepared.effort]}
        else:
            config["thinkingConfig"] = {"thinkingLevel": _LEVEL[prepared.effort]}
    if config:
        body["generationConfig"] = config
    return deep_merge(body, prepared.params)


# --- the reply -------------------------------------------------------------------------------

def stream(conn: ConnInfo, prepared: Prepared) -> Iterator[Any]:
    name = prepared.model_id.removeprefix("models/")
    url = _wire.join_url(conn.base_url, f"models/{quote(name, safe='')}:streamGenerateContent") + "?alt=sse"
    quirks = dict(conn.quirks)
    usage: dict[str, Any] = {}
    reported: str | None = None
    finish: str | None = None
    mapping: dict[str, str] = {}
    called = False

    with _wire.post_stream(url, headers=_auth(conn), body=body_for(prepared, quirks)) as response:
        for _, data in _wire.iter_sse(response):
            chunk = _wire.loads_event(data, url)
            if not isinstance(chunk, dict):
                continue
            if isinstance(chunk.get("error"), dict):
                raise _wire.error_in_body(chunk, url)
            reported = chunk.get("modelVersion") or reported
            usage.update(chunk.get("usageMetadata") or {})
            block = (chunk.get("promptFeedback") or {}).get("blockReason")
            if block and not chunk.get("candidates"):
                raise errors.ContentRefused(f"Google declined this request ({block}).")
            for candidate in chunk.get("candidates") or []:
                finish = candidate.get("finishReason") or finish
                for part in (candidate.get("content") or {}).get("parts") or []:
                    if not isinstance(part, dict):
                        continue
                    if part.get("thoughtSignature"):
                        yield SealedEvent("thought_signature", {"signature": part["thoughtSignature"]})
                    if part.get("thought"):
                        if part.get("text"):
                            yield ReasoningDelta(part["text"])
                        continue
                    if part.get("text"):
                        yield TextDelta(part["text"])
                    elif isinstance(part.get("functionCall"), dict):
                        call = part["functionCall"]
                        if not call.get("name"):
                            raise errors.Unavailable("The model asked to use a tool without naming it, so nothing "
                                                     "was run.")
                        cid = prepared.mint_id()
                        called = True
                        if call.get("id"):
                            mapping[cid] = call["id"]
                        args = call.get("args") if isinstance(call.get("args"), dict) else {}
                        yield ToolCallStarted(cid, call["name"])
                        yield ToolArgsDelta(cid, _wire.dumps(args))
                        yield ToolCallCompleted(ToolCall(cid, call["name"], args))

    if finish is None:
        raise errors.Unavailable(f"The reply from {_wire.host_of(url)} stopped part-way.")
    if finish == "MALFORMED_FUNCTION_CALL":
        raise errors.Unavailable("The model tried to use a tool but produced a broken request, so nothing was run.")
    if mapping:
        yield SealedEvent("ids", mapping)
    if called:
        stop = "tool_calls"
    elif finish in _BLOCKED:
        stop = "content_filter"
    else:
        stop = _FINISH.get(finish, "stop")
    yield Finish(stop, Usage(input=_wire.count(usage.get("promptTokenCount")),
                             output=_wire.count(usage.get("candidatesTokenCount")),
                             cached=_wire.count(usage.get("cachedContentTokenCount")),
                             reasoning=_wire.count(usage.get("thoughtsTokenCount")), raw=usage), reported)


# --- discovery -------------------------------------------------------------------------------

def discover(conn: ConnInfo) -> list[Discovered]:
    """Only models Google itself says can `generateContent`."""
    found: list[Discovered] = []
    token: str | None = None
    for _ in range(20):
        params: dict[str, Any] = {"pageSize": 1000}
        if token:
            params["pageToken"] = token
        body = _wire.get_json(_wire.join_url(conn.base_url, "models"), headers=_auth(conn), params=params)
        rows = body.get("models") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            raise errors.InvalidRequest(f"{_wire.host_of(conn.base_url)} answered, but not with a list of models.")
        for row in rows:
            if not isinstance(row, dict) or not row.get("name"):
                continue
            methods = row.get("supportedGenerationMethods")
            if isinstance(methods, list) and "generateContent" not in methods:
                continue
            caps: dict[str, Any] = {}
            if _wire.count(row.get("inputTokenLimit")):
                caps["max_context_tokens"] = row["inputTokenLimit"]
            if _wire.count(row.get("outputTokenLimit")):
                caps["max_output_tokens"] = row["outputTokenLimit"]
            if isinstance(row.get("thinking"), bool):
                caps["reasoning_control"] = row["thinking"]
            model_id = str(row["name"]).removeprefix("models/")
            if "tts" in model_id.lower().split("-"):
                # Speech only: it answers in audio and refuses a text reply. The list says nothing
                # about output kinds, so the id Google gives these models is the one signal.
                caps["text_in"] = False
            found.append(Discovered(model_id=model_id,
                                    label=row.get("displayName") or None, capabilities=caps, family="gemini"))
        token = body.get("nextPageToken")
        if not token:
            break
    return found
