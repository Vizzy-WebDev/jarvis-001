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

import re
from typing import Any, Iterator, Mapping
from urllib.parse import quote

from ..errors import ProviderError
from ..request import REASONING_LEVELS, ChatRequest, ImagePart, MediaPart, Message, ToolCallPart, ToolResultPart
from ..types import CheckResult, Discovered, Finished, RawError, TextDelta, ToolUse, Usage, Target
from . import _wire as wire

FORMAT = "gemini-generatecontent"

_FINISH = {"STOP": "stop", "MAX_TOKENS": "length"}
_BLOCKED = {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY", "LANGUAGE"}

#: The parts of JSON Schema Gemini accepts in a function's parameters.
_SCHEMA_KEYS = {"type", "format", "description", "nullable", "enum", "properties", "required",
                "items", "minItems", "maxItems", "minimum", "maximum", "minLength", "maxLength", "pattern"}
#: A generated call id, to tell it from one Gemini itself supplied.
_GENERATED = "gemini_call_"

#: The neutral reasoning levels as a thinking budget in tokens — inside what every
#: Gemini model that reports `thinking` accepts (the smallest ceiling is 24576).
_THINKING_BUDGET = {"minimal": 1024, "balanced": 4096, "thorough": 16384, "maximum": 24576}

#: Google's `error.status` -> (kind, scope, worth trying again). Read before the HTTP
#: status, because the status name is what Google itself says happened.
_ERRORS = {
    "RESOURCE_EXHAUSTED": ("rate", "model", True),
    "PERMISSION_DENIED": ("auth", "credential", False),
    "UNAUTHENTICATED": ("auth", "credential", False),
    "INVALID_ARGUMENT": ("request", "request", False),
    "FAILED_PRECONDITION": ("billing", "credential", False),
    "NOT_FOUND": ("model", "model", False),
    "UNAVAILABLE": ("overloaded", "provider", True),
    "INTERNAL": ("server", "provider", True),
    "DEADLINE_EXCEEDED": ("server", "provider", True),
}
#: `ErrorInfo.reason` values that mean the KEY is wrong, whatever status came with them
#: (Google answers a bad key with 400 INVALID_ARGUMENT).
_KEY_REASONS = {"API_KEY_INVALID", "API_KEY_EXPIRED", "API_KEY_SERVICE_BLOCKED"}
#: How Google says a prompt didn't fit THIS model's context window.
_TOO_LONG = re.compile(r"exceeds the maximum number of tokens|input token count", re.IGNORECASE)


def _details(err: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [d for d in err.get("details") or []
            if isinstance(d, dict) and str(d.get("@type", "")).endswith(kind)]


def normalize_error(raw: RawError) -> ProviderError:
    """Google's failure, read from `error.status` and its `details` — the same shape
    whether it was the HTTP answer or a frame inside an otherwise-200 stream."""
    body = raw.body if isinstance(raw.body, dict) else {}
    err = body.get("error") if isinstance(body.get("error"), dict) else {}
    code = err.get("code")
    if raw.status is None and isinstance(code, int) and not isinstance(code, bool):
        raw = RawError(status=code, headers=raw.headers, body=raw.body, words=raw.words, url=raw.url)
    retry = None
    for info in _details(err, "google.rpc.RetryInfo"):
        retry = wire.duration_s(info.get("retryDelay"))
    retry = retry if retry is not None else wire.retry_after_s(raw.headers)
    if any(d.get("reason") in _KEY_REASONS for d in _details(err, "google.rpc.ErrorInfo")):
        return wire.make(raw, kind="auth", scope="credential")
    name = str(err.get("status") or "")
    if name == "INVALID_ARGUMENT" and _TOO_LONG.search(raw.words):
        return wire.make(raw, kind="request", scope="model")  # too long for THIS model's window
    if name in _ERRORS:
        kind, scope, again = _ERRORS[name]
        return wire.make(raw, kind=kind, scope=scope, retryable=again, retry_after_s=retry if again else None)
    if raw.status is None:
        return wire.make(raw, kind="server", scope="unknown",
                         message=f"Google stopped the reply: {raw.words or 'it reported an error.'}")
    return wire.fallback(raw)


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
                             params=params, missing="address", normalize=normalize_error)
        rows = body.get("models") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            raise ProviderError("Google answered, but not with a list of models.", kind="request",
                                scope="provider")
        found.extend(r for r in rows if isinstance(r, dict) and r.get("name"))
        token = body.get("nextPageToken")
        if not token:
            break
    return found


def check(target: Target) -> CheckResult:
    body = wire.get_json(wire.join_url(target.base_url, "models"), headers=_auth(target),
                         params={"pageSize": 1}, missing="address", normalize=normalize_error)
    if not isinstance(body, dict) or "models" not in body:
        raise ProviderError("Google answered, but not with a list of models.", kind="request", scope="provider")
    return CheckResult(True, "Connected to Google.")


def discover(target: Target) -> list[Discovered]:
    """Only models Google itself says can `generateContent` — that is its own
    report, not a judgement made here — so embedding-only and similar models
    don't crowd the list. Whether a model thinks is Google's own `thinking` flag,
    kept only where it gave one."""
    found = []
    for row in _listing(target, 1000):
        methods = row.get("supportedGenerationMethods")
        if isinstance(methods, list) and "generateContent" not in methods:
            continue
        name = str(row["name"])
        facts: dict[str, Any] = {}
        if isinstance(row.get("thinking"), bool):
            facts["reasoning"] = ({"supported": True, "levels": list(REASONING_LEVELS), "default": None}
                                  if row["thinking"] else {"supported": False})
        found.append(Discovered(model_id=name.removeprefix("models/"), label=row.get("displayName") or None,
                                facts=facts or None, raw=row))
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


def _user_parts(message: Message) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    text = message.text()
    if text:
        parts.append({"text": text})
    for part in message.parts:  # Gemini takes any attachment it has the bytes of, in order
        if isinstance(part, (ImagePart, MediaPart)):
            parts.append({"inlineData": {"mimeType": part.mime_type, "data": part.data_base64}})
    return parts


def _model_parts(message: Message) -> list[dict[str, Any]]:
    raw = message.replay
    if isinstance(raw, dict) and raw.get("adapter") == FORMAT and isinstance(raw.get("parts"), list):
        return [dict(p) for p in raw["parts"] if isinstance(p, dict)]
    parts: list[dict[str, Any]] = []
    text = message.text()
    if text:
        parts.append({"text": text})
    for call in message.of(ToolCallPart):
        parts.append({"functionCall": {"name": call.name, "args": call.args}})
    return parts


def _contents(messages: tuple[Message, ...]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "user":
            side, parts = "user", _user_parts(message)
        elif message.role == "assistant":
            side, parts = "model", _model_parts(message)
        elif message.role == "tool":
            side, parts = "user", []
            for result in message.of(ToolResultPart):
                value = result.result
                response = {"functionResponse": {
                    "name": result.name,
                    "response": value if isinstance(value, dict) else {"result": value}}}
                if not str(result.call_id).startswith(_GENERATED):
                    response["functionResponse"]["id"] = result.call_id
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


_TOOL_MODE = {"auto": "AUTO", "required": "ANY", "none": "NONE"}


def _body(request: ChatRequest, facts: Mapping[str, Any] | None) -> dict[str, Any]:
    body: dict[str, Any] = {"contents": _contents(request.messages)}
    instruction = wire.flatten_system(request.system)
    if instruction:
        body["systemInstruction"] = {"parts": [{"text": instruction}]}
    if request.tools:
        body["tools"] = [{"functionDeclarations": [
            {"name": t.name, "description": t.description, "parameters": _schema(t.parameters)}
            for t in request.tools]}]
        if request.options.tool_choice:
            body["toolConfig"] = {"functionCallingConfig": {"mode": _TOOL_MODE[request.options.tool_choice]}}
    config: dict[str, Any] = {}
    options = request.options
    if options.temperature is not None:
        config["temperature"] = options.temperature
    if options.max_output_tokens:
        config["maxOutputTokens"] = options.max_output_tokens
    if options.response_format == "json":
        config["responseMimeType"] = "application/json"
    level = wire.reasoning_level(request, facts)
    if level:
        config["thinkingConfig"] = {"thinkingBudget": _THINKING_BUDGET[level]}
    if config:
        body["generationConfig"] = config
    return body


def stream(target: Target, request: ChatRequest, *,
           facts: Mapping[str, Any] | None = None) -> Iterator[Any]:
    body = _body(request, facts)
    name = request.model_id.removeprefix("models/")
    url = wire.join_url(target.base_url, f"models/{quote(name, safe='')}:streamGenerateContent") + "?alt=sse"

    parts: list[dict[str, Any]] = []
    reported: str | None = None
    finish: str | None = None
    usage: dict[str, Any] = {}
    seen_any = False

    with wire.post_stream(url, headers=_auth(target), body=body, normalize=normalize_error) as response:
        for _, data in wire.iter_sse(response):
            chunk = wire.loads_event(data, "Google")
            if not isinstance(chunk, dict):
                continue
            if isinstance(chunk.get("error"), dict):
                # Google reports a mid-stream failure inside an otherwise-200 stream.
                # Without this it fell through to "stopped part-way" (kind="reply"),
                # losing what Google said happened — which is what Auto's cooldowns
                # and holds are keyed off. The same reading as an HTTP failure.
                raise normalize_error(wire.in_band(chunk, url))
            seen_any = True
            reported = chunk.get("modelVersion") or reported
            usage.update(chunk.get("usageMetadata") or {})
            block = (chunk.get("promptFeedback") or {}).get("blockReason")
            if block and not chunk.get("candidates"):
                raise ProviderError(f"Google declined this request ({block}).", kind="request", scope="request")
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
        raise ProviderError("Google sent no reply.", kind="reply", scope="model")
    if finish is None:
        # Every finished reply ends with a finish reason. One that just stops is not
        # finished, and saying so beats a confident half-answer.
        raise ProviderError("The reply from Google stopped part-way.", kind="reply", scope="model")
    if finish == "MALFORMED_FUNCTION_CALL":
        raise ProviderError("The model tried to use a tool but produced a broken request, so nothing was run.",
                            kind="reply", scope="model")

    tool_calls = []
    for index, part in enumerate(p for p in parts if "functionCall" in p):
        call = part["functionCall"]
        if not call.get("name"):
            raise ProviderError("The model asked to use a tool without naming it, so nothing was run.", kind="reply",
                                scope="model")
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
        usage=Usage(tokens_in=wire.count(usage.get("promptTokenCount")),
                    tokens_out=wire.count(usage.get("candidatesTokenCount")),
                    tokens_reasoning=wire.count(usage.get("thoughtsTokenCount")),
                    cached_in=wire.count(usage.get("cachedContentTokenCount"))) if usage else None,
        model_id=reported,
        raw={"adapter": FORMAT, "parts": parts},
    )
