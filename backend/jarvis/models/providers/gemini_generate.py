"""Google Gemini's own API — `generateContent`, streamed.

Two Gemini-specific rules shape this module:

* A reply's parts carry a `thoughtSignature` that must come back verbatim on the
  next request, or a tool-calling turn is refused. The reply's parts are kept
  exactly as received in `raw` and replayed as-is.
* Tool parameters are an OpenAPI-style *subset* of JSON Schema. Keywords outside
  it are refused outright, so a schema is translated down to what Gemini accepts.

The key travels in the `x-goog-api-key` header rather than the `?key=` query
form, so it is never part of a URL that can end up in a log or an error message.
"""

from __future__ import annotations

from typing import Any, Iterator
from urllib.parse import quote

from ..errors import ProviderError
from ..types import CheckResult, Discovered, Finished, TextDelta, ToolUse, Usage, Target
from . import _wire as wire

FORMAT = "gemini-generatecontent"

_FINISH = {"STOP": "stop", "MAX_TOKENS": "length"}
_BLOCKED = {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY", "LANGUAGE"}

#: The parts of JSON Schema Gemini accepts in a function's parameters.
_SCHEMA_KEYS = {"type", "format", "description", "nullable", "enum", "properties", "required",
                "items", "minItems", "maxItems", "minimum", "maximum", "minLength", "maxLength", "pattern"}
#: A generated call id, to tell it from one Gemini itself supplied.
_GENERATED = "gemini_call_"


def _auth(target: Target) -> dict[str, str]:
    return {"x-goog-api-key": target.api_key} if target.api_key else {}


def _listing(target: Target, page_size: int) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    token: str | None = None
    for _ in range(20):  # a bound, so a provider that never says "no more" can't loop forever
        params: dict[str, Any] = {"pageSize": page_size}
        if token:
            params["pageToken"] = token
        body = wire.get_json(wire.join_url(target.base_url, "models"), headers=_auth(target),
                             params=params, missing="address")
        rows = body.get("models") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            raise ProviderError("Google answered, but not with a list of models.", kind="request")
        found.extend(r for r in rows if isinstance(r, dict) and r.get("name"))
        token = body.get("nextPageToken")
        if not token:
            break
    return found


def check(target: Target) -> CheckResult:
    body = wire.get_json(wire.join_url(target.base_url, "models"), headers=_auth(target),
                         params={"pageSize": 1}, missing="address")
    if not isinstance(body, dict) or "models" not in body:
        raise ProviderError("Google answered, but not with a list of models.", kind="request")
    return CheckResult(True, "Connected to Google.")


def discover(target: Target) -> list[Discovered]:
    """Only models Google itself says can `generateContent` — that is its own
    report, not a judgement made here — so embedding-only and similar models
    don't crowd the list."""
    found = []
    for row in _listing(target, 1000):
        methods = row.get("supportedGenerationMethods")
        if isinstance(methods, list) and "generateContent" not in methods:
            continue
        name = str(row["name"])
        found.append(Discovered(model_id=name.removeprefix("models/"), label=row.get("displayName") or None))
    return found


# --- the conversation, in this format ------------------------------------------------

def _schema(node: Any) -> Any:
    """JSON Schema narrowed to what Gemini takes."""
    if isinstance(node, list):
        return [_schema(n) for n in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key not in _SCHEMA_KEYS:
            continue
        if key == "type" and isinstance(value, list):
            kinds = [v for v in value if v != "null"]
            out["type"] = kinds[0] if kinds else "string"
            if "null" in value:
                out["nullable"] = True
        elif key == "properties" and isinstance(value, dict):
            out["properties"] = {name: _schema(sub) for name, sub in value.items()}
        elif key == "items":
            out["items"] = _schema(value)
        else:
            out[key] = value
    # JSON Schema lets an array leave its element type unsaid; Gemini refuses one that
    # does ("items: missing field") and, with it, the whole request — so every turn
    # that declared the tool. Strings are what tool arguments are made of here, and a
    # working turn with a stated guess is better than a refused one. A tool that means
    # something else says so in its own schema, which is always respected.
    if out.get("type") == "array" and not out.get("items"):
        out["items"] = {"type": "string"}
    return out


def _user_parts(message: dict[str, Any]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    if message.get("text"):
        parts.append({"text": message["text"]})
    for _, mime, data in wire.media_of(message):
        parts.append({"inlineData": {"mimeType": mime, "data": data}})
    return parts


def _model_parts(message: dict[str, Any]) -> list[dict[str, Any]]:
    raw = message.get("raw")
    if (isinstance(raw, dict) and raw.get("adapter") == FORMAT and isinstance(raw.get("parts"), list)
            and not message.get("interrupted")):
        return [dict(p) for p in raw["parts"] if isinstance(p, dict)]
    parts: list[dict[str, Any]] = []
    text = wire.assistant_text(message)
    if text:
        parts.append({"text": text})
    for call in message.get("toolCalls") or []:
        parts.append({"functionCall": {"name": call["name"], "args": call.get("args") or {}}})
    return parts


def _contents(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "user":
            side, parts = "user", _user_parts(message)
        elif role == "assistant":
            side, parts = "model", _model_parts(message)
        elif role == "tool":
            side, parts = "user", []
            for result in message.get("toolResults") or []:
                value = result.get("result")
                response = {"functionResponse": {
                    "name": result["name"],
                    "response": value if isinstance(value, dict) else {"result": value}}}
                if not str(result["id"]).startswith(_GENERATED):
                    response["functionResponse"]["id"] = result["id"]
                parts.append(response)
        else:
            continue
        if not parts:
            continue
        if out and out[-1]["role"] == side:
            out[-1]["parts"].extend(parts)
        else:
            out.append({"role": side, "parts": parts})
    while out and out[0]["role"] != "user":  # a trimmed window can open on a reply
        out.pop(0)
    return out


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def stream(target: Target, *, model_id: str, messages: list[dict[str, Any]], system: str,
           tools: list[dict[str, Any]], effort: str | None = None,
           facts: dict[str, Any] | None = None) -> Iterator[Any]:
    """`effort` is deliberately unused: Google's model list reports no reasoning
    capabilities, so no level is offered for these models and none is sent."""
    body: dict[str, Any] = {"contents": _contents(messages)}
    instruction = wire.flatten_system(system)
    if instruction:
        body["systemInstruction"] = {"parts": [{"text": instruction}]}
    if tools:
        body["tools"] = [{"functionDeclarations": [
            {"name": t["name"], "description": t.get("description", ""),
             "parameters": _schema(t.get("parameters") or {"type": "object", "properties": {}})}
            for t in tools]}]

    name = model_id.removeprefix("models/")
    url = wire.join_url(target.base_url, f"models/{quote(name, safe='')}:streamGenerateContent") + "?alt=sse"

    parts: list[dict[str, Any]] = []
    reported: str | None = None
    finish: str | None = None
    usage: dict[str, Any] = {}
    seen_any = False

    with wire.post_stream(url, headers=_auth(target), body=body,
                          read_timeout=target.read_timeout) as response:
        for _, data in wire.iter_sse(response):
            chunk = wire.loads_event(data, "Google")
            if not isinstance(chunk, dict):
                continue
            seen_any = True
            reported = chunk.get("modelVersion") or reported
            usage.update(chunk.get("usageMetadata") or {})
            block = (chunk.get("promptFeedback") or {}).get("blockReason")
            if block and not chunk.get("candidates"):
                raise ProviderError(f"Google declined this request ({block}).", kind="request")
            for candidate in chunk.get("candidates") or []:
                finish = candidate.get("finishReason") or finish
                for part in (candidate.get("content") or {}).get("parts") or []:
                    if not isinstance(part, dict):
                        continue
                    plain = ("text" in part and not part.get("thought") and "thoughtSignature" not in part
                             and len(part) == 1)
                    if plain and parts and set(parts[-1]) == {"text"}:
                        parts[-1]["text"] += part["text"]  # adjacent plain text reads as one part
                    else:
                        parts.append(dict(part))
                    if part.get("text") and not part.get("thought"):
                        yield TextDelta(part["text"])

    if not seen_any:
        raise ProviderError("Google sent no reply.", kind="reply")
    if finish is None:
        # Every finished reply ends with a finish reason. One that just stops is not
        # finished, and saying so beats a confident half-answer.
        raise ProviderError("The reply from Google stopped part-way.", kind="reply")
    if finish == "MALFORMED_FUNCTION_CALL":
        raise ProviderError("The model tried to use a tool but produced a broken request, so nothing was run.",
                            kind="reply")

    tool_calls = []
    for index, part in enumerate(p for p in parts if "functionCall" in p):
        call = part["functionCall"]
        if not call.get("name"):
            raise ProviderError("The model asked to use a tool without naming it, so nothing was run.", kind="reply")
        tool_calls.append(ToolUse(id=call.get("id") or f"{_GENERATED}{index}", name=call["name"],
                                  args=call.get("args") if isinstance(call.get("args"), dict) else {}))
    text = "".join(p.get("text", "") for p in parts if "text" in p and not p.get("thought"))

    if tool_calls:
        reason = "tool_calls"
    elif finish in _BLOCKED:
        reason = "content_filter"
    else:
        reason = _FINISH.get(finish or "STOP", "stop")

    yield Finished(
        text=text, tool_calls=tuple(tool_calls), finish_reason=reason,
        usage=Usage(tokens_in=_count(usage.get("promptTokenCount")),
                    tokens_out=_count(usage.get("candidatesTokenCount")),
                    tokens_reasoning=_count(usage.get("thoughtsTokenCount")),
                    cached_in=_count(usage.get("cachedContentTokenCount"))) if usage else None,
        model_id=reported,
        raw={"adapter": FORMAT, "parts": parts},
    )
