"""OpenAI's own API — the Responses endpoint.

A first-class integration, written for OpenAI's format alone. It is not a gateway
other providers are routed through: Ollama, LM Studio and the like speak the
older chat format and have their own module (`openai_chat.py`).

Requests are sent with `store: false`. Jarvis keeps the conversation itself and
resends it in full each turn, so there is no reason for OpenAI to keep a second
copy — and the history is rebuilt from Jarvis's own records rather than from
OpenAI's item ids, which do not resolve once nothing is stored.
"""

from __future__ import annotations

from typing import Any, Iterator, Mapping

from ..errors import ProviderError, Unsupported
from ..request import ChatRequest, ImagePart, MediaPart, Message, ToolCallPart, ToolResultPart
from ..types import CheckResult, Discovered, Finished, RawError, TextDelta, ToolUse, Usage, Target
from . import _wire as wire

FORMAT = "openai-responses"

_INCOMPLETE = {"max_output_tokens": "length", "content_filter": "content_filter"}

#: The neutral reasoning levels in OpenAI's `reasoning.effort` names. Sent only to a
#: model reported to reason — and OpenAI's model list reports nothing of the kind, so
#: today that is only a model whose facts were stated some other way.
_REASONING = {"minimal": "low", "balanced": "medium", "thorough": "high", "maximum": "high"}


def _auth(target: Target) -> dict[str, str]:
    return {"Authorization": f"Bearer {target.api_key}"} if target.api_key else {}


# --- failures ---------------------------------------------------------------------------

def normalize_error(raw: RawError) -> ProviderError:
    """OpenAI's failure, read from `error.code` / `error.type` first and its HTTP
    status only when those say nothing this module recognises."""
    body = raw.body if isinstance(raw.body, dict) else {}
    err = body.get("error") if isinstance(body.get("error"), dict) else body
    code = str(err.get("code") or "").lower()
    kind_name = str(err.get("type") or "").lower()
    names = (code, kind_name)
    retry = wire.retry_after_s(raw.headers) or wire.reset_headers_s(raw.headers)

    if "insufficient_quota" in names:
        return wire.make(raw, kind="billing", scope="provider")
    if "context_length_exceeded" in names:
        # Too long for THIS model's window — a model with a larger one may still take it.
        return wire.make(raw, kind="request", scope="model")
    if "rate_limit_exceeded" in names or raw.status == 429:
        return wire.make(raw, kind="rate", scope="model", retryable=True, retry_after_s=retry)
    if raw.status == 401 or "invalid_api_key" in names:
        return wire.make(raw, kind="auth", scope="credential")
    if "model_not_found" in names:
        return wire.make(raw, kind="model", scope="model")
    if "server_error" in names or (raw.status or 0) >= 500:
        if raw.status is None:
            return wire.make(raw, kind="server", scope="provider", retryable=True,
                             message=f"OpenAI couldn't finish the reply. {raw.words}".strip())
        return wire.make(raw, kind="server", scope="provider", retryable=True, retry_after_s=retry)
    if raw.status is None:
        return wire.make(raw, kind="server", scope="unknown",
                         message=f"OpenAI stopped the reply. {raw.words}".strip())
    return wire.fallback(raw)


# --- the model list -----------------------------------------------------------------------

def _listing(target: Target) -> list[dict[str, Any]]:
    body = wire.get_json(wire.join_url(target.base_url, "models"), headers=_auth(target), missing="address",
                         normalize=normalize_error)
    rows = body.get("data") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        raise ProviderError("OpenAI answered, but not with a list of models.", kind="request", scope="provider")
    rows = [r for r in rows if isinstance(r, dict) and r.get("id")]
    rows.sort(key=lambda r: r.get("created") or 0, reverse=True)
    return rows


def _key_works_without_listing(target: Target) -> bool:
    """A restricted key can be allowed to generate but not to list models. Whether
    the key is accepted at all is answered by the generation endpoint, which
    complains about the empty request (400) only once the key is accepted."""
    raw = wire.probe_post(wire.join_url(target.base_url, "responses"), headers=_auth(target), body={})
    if raw.status in (401, 403, 404) or (raw.status or 0) >= 500:
        raise normalize_error(raw)
    return True


def check(target: Target) -> CheckResult:
    try:
        count = len(_listing(target))
    except ProviderError as err:
        if err.kind != "forbidden":
            raise
        _key_works_without_listing(target)
        return CheckResult(True, "Connected. This key isn't allowed to list models, so add the model "
                                 "IDs you want by hand.")
    return CheckResult(True, f"Connected. {count} model{'s' if count != 1 else ''} available.")


def discover(target: Target) -> list[Discovered]:
    """Everything OpenAI lists — chat models and otherwise, because the list says
    nothing about which is which. Picking one that can't chat is answered by
    OpenAI's own error when it is used, not decided here."""
    try:
        rows = _listing(target)
    except ProviderError as err:
        if err.kind == "forbidden":
            raise Unsupported("This key isn't allowed to list models — add the model ID by hand.") from err
        raise
    return [Discovered(model_id=str(r["id"]), raw=r) for r in rows]


# --- the conversation, in this format ------------------------------------------------

def _user(message: Message) -> dict[str, Any] | None:
    text = message.text()
    media = message.of(MediaPart)
    if media:
        raise ProviderError(f"OpenAI can take images here, but not {media[0].kind} attachments.",
                            kind="request", scope="model")
    images = message.of(ImagePart)
    if not images:
        return {"role": "user", "content": text} if text else None
    parts: list[dict[str, Any]] = [{"type": "input_text", "text": text}] if text else []
    parts.extend({"type": "input_image", "image_url": f"data:{i.mime_type};base64,{i.data_base64}"}
                 for i in images)
    return {"role": "user", "content": parts}


def _input(messages: tuple[Message, ...]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "user":
            entry = _user(message)
            if entry:
                items.append(entry)
        elif message.role == "assistant":
            text = message.text()
            if text:
                items.append({"role": "assistant", "content": text})
            for call in message.of(ToolCallPart):
                items.append({"type": "function_call", "call_id": call.id, "name": call.name,
                              "arguments": wire.dumps(call.args)})
        elif message.role == "tool":
            for result in message.of(ToolResultPart):
                items.append({"type": "function_call_output", "call_id": result.call_id,
                              "output": wire.dumps(result.result)})
    return items


def _usage(raw: dict[str, Any]) -> Usage:
    return Usage(
        tokens_in=wire.count(raw.get("input_tokens")),
        tokens_out=wire.count(raw.get("output_tokens")),
        tokens_reasoning=wire.count((raw.get("output_tokens_details") or {}).get("reasoning_tokens")),
        cached_in=wire.count((raw.get("input_tokens_details") or {}).get("cached_tokens")),
    )


def _body(request: ChatRequest, facts: Mapping[str, Any] | None) -> dict[str, Any]:
    body: dict[str, Any] = {"model": request.model_id, "input": _input(request.messages), "stream": True,
                            "store": False}
    instructions = wire.flatten_system(request.system)
    if instructions:
        body["instructions"] = instructions
    if request.tools:
        # `strict` defaults to true on this API, which rejects any schema that
        # isn't closed and fully required — that is not how Jarvis's are written.
        body["tools"] = [{"type": "function", "name": t.name, "description": t.description,
                          "parameters": t.parameters, "strict": False} for t in request.tools]
        if request.options.tool_choice:
            body["tool_choice"] = request.options.tool_choice
    options = request.options
    if options.temperature is not None:
        body["temperature"] = options.temperature
    if options.max_output_tokens:
        body["max_output_tokens"] = options.max_output_tokens
    if options.response_format == "json":
        body["text"] = {"format": {"type": "json_object"}}
    level = wire.reasoning_level(request, facts)
    if level:
        body["reasoning"] = {"effort": _REASONING[level]}
    return body


def stream(target: Target, request: ChatRequest, *,
           facts: Mapping[str, Any] | None = None) -> Iterator[Any]:
    url = wire.join_url(target.base_url, "responses")
    body = _body(request, facts)

    streamed: list[str] = []
    final: dict[str, Any] | None = None

    with wire.post_stream(url, headers=_auth(target), body=body, normalize=normalize_error) as response:
        for event, data in wire.iter_sse(response):
            payload = wire.loads_event(data, "OpenAI")
            if not isinstance(payload, dict):
                continue
            kind = payload.get("type") or event
            if kind == "response.output_text.delta":
                piece = payload.get("delta")
                if piece:
                    streamed.append(piece)
                    yield TextDelta(piece)
            elif kind in ("response.completed", "response.incomplete"):
                final = payload.get("response") or {}
                break
            elif kind == "response.failed":
                failed = (payload.get("response") or {}).get("error") or {}
                raise normalize_error(wire.in_band({"error": failed if isinstance(failed, dict)
                                                    else {"message": str(failed)}}, url))
            elif kind == "error":
                raise normalize_error(wire.in_band({"error": payload}, url))

    if final is None:
        raise ProviderError("The reply from OpenAI stopped part-way.", kind="reply", scope="model")

    said: list[str] = []
    tool_calls: list[ToolUse] = []
    for item in final.get("output") or []:
        if item.get("type") == "function_call":
            tool_calls.append(ToolUse(id=item.get("call_id") or item.get("id") or "",
                                      name=item.get("name") or "",
                                      args=wire.parse_arguments(item.get("arguments"), item.get("name") or "a tool")))
        elif item.get("type") == "message":
            for part in item.get("content") or []:
                if part.get("type") == "output_text":
                    said.append(part.get("text") or "")
                elif part.get("type") == "refusal":
                    said.append(part.get("refusal") or "")
    if any(not call.name or not call.id for call in tool_calls):
        raise ProviderError("The model asked to use a tool without naming it, so nothing was run.",
                            kind="reply", scope="model")

    if final.get("status") == "incomplete":
        why = (final.get("incomplete_details") or {}).get("reason")
        reason = _INCOMPLETE.get(why or "", "length")
    else:
        reason = "tool_calls" if tool_calls else "stop"

    yield Finished(
        text="".join(streamed) or "".join(said),
        tool_calls=tuple(tool_calls), finish_reason=reason,
        usage=_usage(final["usage"]) if isinstance(final.get("usage"), dict) else None,
        model_id=final.get("model"),
    )
