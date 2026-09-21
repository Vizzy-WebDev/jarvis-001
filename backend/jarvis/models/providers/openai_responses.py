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

from typing import Any, Iterator

from ..errors import ProviderError, Unsupported
from ..types import CheckResult, Discovered, Finished, TextDelta, ToolUse, Usage, Target
from . import _wire as wire

FORMAT = "openai-responses"

_INCOMPLETE = {"max_output_tokens": "length", "content_filter": "content_filter"}


def _auth(target: Target) -> dict[str, str]:
    return {"Authorization": f"Bearer {target.api_key}"} if target.api_key else {}


def _listing(target: Target) -> list[dict[str, Any]]:
    body = wire.get_json(wire.join_url(target.base_url, "models"), headers=_auth(target), missing="address")
    rows = body.get("data") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        raise ProviderError("OpenAI answered, but not with a list of models.", kind="request")
    rows = [r for r in rows if isinstance(r, dict) and r.get("id")]
    rows.sort(key=lambda r: r.get("created") or 0, reverse=True)
    return rows


def _key_works_without_listing(target: Target) -> bool:
    """A restricted key can be allowed to generate but not to list models. Whether
    the key is accepted at all is answered by the generation endpoint, which
    complains about the empty request (400) only once the key is accepted."""
    status, words = wire.probe_post(wire.join_url(target.base_url, "responses"), headers=_auth(target), body={})
    if status in (401, 403, 404) or status >= 500:
        raise wire.error_for(status, words, target.base_url)
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
    return [Discovered(model_id=str(r["id"])) for r in rows]


# --- the conversation, in this format ------------------------------------------------

def _user(message: dict[str, Any]) -> dict[str, Any] | None:
    text = message.get("text") or ""
    media = wire.media_of(message)
    if not media:
        return {"role": "user", "content": text} if text else None
    parts: list[dict[str, Any]] = [{"type": "input_text", "text": text}] if text else []
    for kind, mime, data in media:
        if kind != "image":
            raise ProviderError(f"OpenAI can take images here, but not {kind} attachments.", kind="request")
        parts.append({"type": "input_image", "image_url": f"data:{mime};base64,{data}"})
    return {"role": "user", "content": parts}


def _input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "user":
            entry = _user(message)
            if entry:
                items.append(entry)
        elif role == "assistant":
            text = wire.assistant_text(message)
            if text:
                items.append({"role": "assistant", "content": text})
            for call in message.get("toolCalls") or []:
                items.append({"type": "function_call", "call_id": call["id"], "name": call["name"],
                              "arguments": wire.dumps(call.get("args") or {})})
        elif role == "tool":
            for result in message.get("toolResults") or []:
                items.append({"type": "function_call_output", "call_id": result["id"],
                              "output": wire.dumps(result.get("result"))})
    return items


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _usage(raw: dict[str, Any]) -> Usage:
    return Usage(
        tokens_in=_count(raw.get("input_tokens")),
        tokens_out=_count(raw.get("output_tokens")),
        tokens_reasoning=_count((raw.get("output_tokens_details") or {}).get("reasoning_tokens")),
        cached_in=_count((raw.get("input_tokens_details") or {}).get("cached_tokens")),
    )


def _failure(response: dict[str, Any]) -> ProviderError:
    err = response.get("error") or {}
    words = err.get("message") if isinstance(err, dict) else str(err)
    return ProviderError(f"OpenAI couldn't finish the reply. {words or ''}".strip(), kind="server")


def stream(target: Target, *, model_id: str, messages: list[dict[str, Any]], system: str,
           tools: list[dict[str, Any]], effort: str | None = None,
           facts: dict[str, Any] | None = None) -> Iterator[Any]:
    """`effort` is deliberately unused. OpenAI's list endpoint reports no
    reasoning capabilities, so no level is ever offered for these models and none
    is sent — a control nobody can verify the model accepts would be a guess."""
    body: dict[str, Any] = {"model": model_id, "input": _input(messages), "stream": True, "store": False}
    instructions = wire.flatten_system(system)
    if instructions:
        body["instructions"] = instructions
    if tools:
        # `strict` defaults to true on this API, which rejects any schema that
        # isn't closed and fully required — that is not how Jarvis's are written.
        body["tools"] = [{"type": "function", "name": t["name"], "description": t.get("description", ""),
                          "parameters": t.get("parameters") or {"type": "object", "properties": {}},
                          "strict": False} for t in tools]

    streamed: list[str] = []
    final: dict[str, Any] | None = None

    with wire.post_stream(wire.join_url(target.base_url, "responses"), headers=_auth(target), body=body) as response:
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
                raise _failure(payload.get("response") or {})
            elif kind == "error":
                raise ProviderError(f"OpenAI stopped the reply. {payload.get('message') or ''}".strip(),
                                    kind="server")

    if final is None:
        raise ProviderError("The reply from OpenAI stopped part-way.", kind="reply")

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
        raise ProviderError("The model asked to use a tool without naming it, so nothing was run.", kind="reply")

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
