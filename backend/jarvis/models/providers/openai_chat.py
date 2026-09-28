"""OpenAI-compatible Chat Completions — what Ollama, LM Studio and most
self-hosted servers speak, and what a `custom` connection uses when its provider
says it is OpenAI-compatible.

This is NOT OpenAI's own integration (`openai_responses.py`). It exists for the
servers that copied the older, simpler chat format, and it stays honest about
what that means: it reads nothing from a model list but the ids. What a GATEWAY
says about its models beyond that (OpenRouter's pricing and modalities, OmniRoute's
router entries) is read by that gateway's own module in `models/gateways/`, for a
connection that declares it — never here. Each listing row is handed on untouched
(`Discovered.raw`) for exactly that.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterator, Mapping

from ..errors import ProviderError, Unsupported
from ..request import ChatRequest, ImagePart, MediaPart, Message, ToolCallPart, ToolResultPart
from ..types import CheckResult, Discovered, Finished, RawError, TextDelta, ToolUse, Usage, Target
from . import _wire as wire

FORMAT = "openai-chat"

_FINISH = {"stop": "stop", "tool_calls": "tool_calls", "function_call": "tool_calls",
           "length": "length", "content_filter": "content_filter"}

#: The neutral reasoning levels in the `reasoning_effort` names the reasoning models
#: served this way (GLM, Kimi, DeepSeek) accept. Only ever sent to a model its
#: gateway reported as reasoning-capable.
_REASONING = {"minimal": "low", "balanced": "low", "thorough": "high", "maximum": "max"}

#: Words that make a 429 about money, not about pace — never a reason to wait a
#: minute and try the same account again.
_MONEY = re.compile(r"quota|billing|credit|insufficient|payment", re.IGNORECASE)


def _auth(target: Target) -> dict[str, str]:
    return {"Authorization": f"Bearer {target.api_key}"} if target.api_key else {}


# --- failures ---------------------------------------------------------------------------

def _error_object(body: Any) -> dict[str, Any]:
    err = body.get("error") if isinstance(body, dict) else None
    return err if isinstance(err, dict) else {}


def normalize_error(raw: RawError) -> ProviderError:
    """What this server's failure means, from its body where it said.

    The body is OpenAI's shape where the server copied it (`error.code`, `error.type`);
    a gateway such as OpenRouter also puts the HTTP status in `error.code`. What a
    gateway's upstream did is not guessed at: a server that answers a failure with a
    structured error of its own is speaking about THIS request to THIS model, while
    one that answers with something unparseable (a proxy's HTML page) is broken as a
    whole — which is the only distinction drawn for a 5xx."""
    err = _error_object(raw.body)
    code = err.get("code")
    status = raw.status if raw.status is not None else (
        code if isinstance(code, int) and not isinstance(code, bool) else None)
    name = str(code or "").lower() if not isinstance(code, int) else ""
    kind_name = str(err.get("type") or "").lower()
    said = f"{name} {kind_name} {raw.words}"
    retry = wire.retry_after_s(raw.headers) or wire.reset_headers_s(raw.headers)
    raw = RawError(status=status, headers=raw.headers, body=raw.body, words=raw.words, url=raw.url)

    if "insufficient_quota" in (name, kind_name):
        return wire.make(raw, kind="billing", scope="provider")
    if "context_length_exceeded" in (name, kind_name):
        # Too long for THIS model's window — a model with a larger one may still take it.
        return wire.make(raw, kind="request", scope="model")
    if status == 429 and _MONEY.search(said):
        return wire.make(raw, kind="billing", scope="credential")
    if name == "rate_limit_exceeded" or status == 429 or "rate_limit" in kind_name:
        return wire.make(raw, kind="rate", scope="model", retryable=True, retry_after_s=retry)
    if status == 401 or name == "invalid_api_key":
        return wire.make(raw, kind="auth", scope="credential")
    if status is None:
        return wire.make(raw, kind="server", scope="model",
                         message=f"{wire.host_of(raw.url)} stopped the reply: {raw.words or 'it reported an error.'}")
    if status >= 500:
        structured = bool(err) or (isinstance(raw.body, dict) and bool(raw.body))
        return wire.make(raw, kind="server", scope="model" if structured else "provider",
                         retryable=True, retry_after_s=retry)
    if status in (400, 422):
        # Opaque: a server of this kind (a gateway especially) answers 400 for things
        # that are about one upstream model as often as about the request itself.
        return wire.make(raw, kind="request", scope="unknown")
    return wire.fallback(raw)


# --- the model list -----------------------------------------------------------------------

def _listing(target: Target) -> list[dict[str, Any]]:
    body = wire.get_json(wire.join_url(target.base_url, "models"), headers=_auth(target), missing="address",
                         normalize=normalize_error)
    rows = body.get("data") if isinstance(body, dict) else body
    if not isinstance(rows, list):
        raise ProviderError(f"{wire.host_of(target.base_url)} answered, but not with a list of models.",
                            kind="request", scope="provider")
    return [r for r in rows if isinstance(r, dict) and r.get("id")]


def check(target: Target) -> CheckResult:
    """Validate as far as this kind of server allows: the model list when it has
    one, and otherwise the chat endpoint itself."""
    try:
        count = len(_listing(target))
    except ProviderError as err:
        if err.status != 404:
            raise
        # No model list here. Is this still a chat server, or a wrong address?
        raw = wire.probe_post(wire.join_url(target.base_url, "chat/completions"), headers=_auth(target), body={})
        if raw.status in (401, 403, 404) or (raw.status or 0) >= 500:
            raise wire.fallback(raw, missing="address")
        return CheckResult(True, "Reached the server. It doesn't offer a list of its models, so the "
                                 "key and the model can't be checked until you use it — add a model ID by hand.")
    return CheckResult(True, f"Connected. {count} model{'s' if count != 1 else ''} available.")


def discover(target: Target) -> list[Discovered]:
    """The ids, and nothing claimed about them. Each row goes along as `raw` so a
    connection that declared a gateway can have that gateway's module read it."""
    try:
        rows = _listing(target)
    except ProviderError as err:
        if err.status == 404:
            raise Unsupported("This server doesn't offer a list of its models — add the model ID by hand.") from err
        raise
    return [Discovered(model_id=str(r["id"]), raw=r) for r in rows]


# --- the conversation, in this format ------------------------------------------------

def _user(message: Message) -> dict[str, Any] | None:
    text = message.text()
    media = message.of(MediaPart)
    if media:
        raise ProviderError(f"This kind of connection can take images, but not {media[0].kind} attachments.",
                            kind="request", scope="model")
    images = message.of(ImagePart)
    if not images:
        return {"role": "user", "content": text} if text else None
    parts: list[dict[str, Any]] = [{"type": "text", "text": text}] if text else []
    parts.extend({"type": "image_url", "image_url": {"url": f"data:{i.mime_type};base64,{i.data_base64}"}}
                 for i in images)
    return {"role": "user", "content": parts}


def _assistant(message: Message) -> dict[str, Any] | None:
    text = message.text()
    calls = message.of(ToolCallPart)
    if not text and not calls:
        return None
    entry: dict[str, Any] = {"role": "assistant", "content": text or None}
    if calls:
        entry["tool_calls"] = [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": wire.dumps(c.args)}}
            for c in calls
        ]
    return entry


def _messages(system: str | None, messages: tuple[Message, ...]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    flat = wire.flatten_system(system)
    if flat:
        out.append({"role": "system", "content": flat})
    for message in messages:
        if message.role == "user":
            entry = _user(message)
        elif message.role == "assistant":
            entry = _assistant(message)
        elif message.role == "tool":
            out.extend({"role": "tool", "tool_call_id": r.call_id, "content": wire.dumps(r.result)}
                       for r in message.of(ToolResultPart))
            continue
        else:
            continue
        if entry:
            out.append(entry)
    return out


def _usage(raw: dict[str, Any]) -> Usage:
    return Usage(
        tokens_in=wire.count(raw.get("prompt_tokens")),
        tokens_out=wire.count(raw.get("completion_tokens")),
        tokens_reasoning=wire.count((raw.get("completion_tokens_details") or {}).get("reasoning_tokens")),
        cached_in=wire.count((raw.get("prompt_tokens_details") or {}).get("cached_tokens")),
    )


#: The start of an error envelope. Some gateways (a local router, found live) answer
#: 200 and stream their own failure — `{"error":{"message":...}}` — as the reply's
#: TEXT. A reply that begins like this is held back rather than spoken, and judged
#: once it is complete (`_error_in_text`).
_ERROR_START = re.compile(r'^\s*\{\s*"error"\s*:')
_ERROR_PREFIX = '{"error":'


def _could_be_error_envelope(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    return bool(_ERROR_START.match(text)) or _ERROR_PREFIX.startswith(compact)


def _error_in_text(text: str, url: str) -> ProviderError | None:
    """The failure a whole reply's text actually is, or None when it is a real answer.

    Narrow on purpose: only a reply that is, in its entirety, one JSON object whose
    `error` carries a message. Anything else — JSON that merely has an "error" field,
    prose that mentions an error — is an answer, and is shown as one.
    """
    try:
        body = json.loads(text)
    except ValueError:
        return None
    err = body.get("error") if isinstance(body, dict) else None
    message = err.get("message") if isinstance(err, dict) else None
    if not isinstance(message, str) or not message.strip():
        return None
    judged = normalize_error(wire.in_band(body, url))
    return ProviderError(f"{wire.host_of(url)} sent back an error instead of a reply: {message.strip()}",
                         kind=judged.kind, scope=judged.scope, retryable_elsewhere=judged.retryable_elsewhere,
                         retry_after_s=judged.retry_after_s, status=judged.status)


def _body(request: ChatRequest, facts: Mapping[str, Any] | None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": request.model_id,
        "messages": _messages(request.system, request.messages),
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if request.tools:
        body["tools"] = [{"type": "function",
                          "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                         for t in request.tools]
        if request.options.tool_choice:
            body["tool_choice"] = request.options.tool_choice
    options = request.options
    if options.temperature is not None:
        body["temperature"] = options.temperature
    if options.max_output_tokens:
        body["max_tokens"] = options.max_output_tokens
    if options.response_format == "json":
        body["response_format"] = {"type": "json_object"}
    level = wire.reasoning_level(request, facts)
    if level:
        body["reasoning_effort"] = _REASONING[level]
    return body


def stream(target: Target, request: ChatRequest, *,
           facts: Mapping[str, Any] | None = None) -> Iterator[Any]:
    url = wire.join_url(target.base_url, "chat/completions")
    host = wire.host_of(target.base_url)
    body = _body(request, facts)

    text: list[str] = []
    calls: dict[int, dict[str, str | None]] = {}
    finish: str | None = None
    finished_cleanly = False
    usage: Usage | None = None
    reported: str | None = None
    # Text held back while it could still be a gateway's error envelope (see above).
    held: list[str] = []
    holding = True

    with wire.post_stream(url, headers=_auth(target), body=body, normalize=normalize_error) as response:
        for _, data in wire.iter_sse(response):
            if data.strip() == "[DONE]":
                finished_cleanly = True
                break
            chunk = wire.loads_event(data, host)
            if not isinstance(chunk, dict):
                continue
            if chunk.get("error"):
                raise normalize_error(wire.in_band(chunk, url))
            reported = chunk.get("model") or reported
            if isinstance(chunk.get("usage"), dict):
                usage = _usage(chunk["usage"])
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                piece = delta.get("content")
                if piece:
                    text.append(piece)
                    if holding:
                        held.append(piece)
                        if _could_be_error_envelope("".join(held)):
                            continue
                        holding = False
                        piece = "".join(held)
                    yield TextDelta(piece)
                for part in delta.get("tool_calls") or []:
                    slot = calls.setdefault(part.get("index", 0), {"id": None, "name": None, "args": ""})
                    if part.get("id"):
                        slot["id"] = part["id"]
                    fn = part.get("function") or {}
                    # A name arrives whole, and some servers repeat it on every chunk.
                    if fn.get("name") and not slot["name"]:
                        slot["name"] = fn["name"]
                    slot["args"] = (slot["args"] or "") + (fn.get("arguments") or "")
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]

    if holding and held:
        failure = _error_in_text("".join(held), url)
        if failure is not None:
            raise failure
        yield TextDelta("".join(held))

    if finish is None and not finished_cleanly:
        # A reply that just stops is not a finished reply. Saying so is the
        # difference between "it broke" and a confident half-answer.
        raise ProviderError(f"The reply from {host} stopped part-way.", kind="reply", scope="model")

    tool_calls = []
    for index in sorted(calls):
        slot = calls[index]
        if not slot["name"]:
            raise ProviderError("The model asked to use a tool without naming it, so nothing was run.",
                                kind="reply", scope="model")
        tool_calls.append(ToolUse(id=slot["id"] or f"call_{index}", name=slot["name"],
                                  args=wire.parse_arguments(slot["args"], slot["name"])))

    reason = "tool_calls" if tool_calls else _FINISH.get(finish or "stop", "stop")
    yield Finished(text="".join(text), tool_calls=tuple(tool_calls), finish_reason=reason,
                   usage=usage, model_id=reported)
