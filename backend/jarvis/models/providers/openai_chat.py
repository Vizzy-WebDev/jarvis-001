"""OpenAI-compatible Chat Completions — what Ollama, LM Studio and most
self-hosted servers speak, and what a `custom` connection uses when its provider
says it is OpenAI-compatible.

This is NOT OpenAI's own integration (`openai_responses.py`). It exists for the
servers that copied the older, simpler chat format, and it stays honest about
what that means: no reasoning controls, and no model-capability data of its own — a
plain server of this kind lists its models by name and says nothing more about them.
(A gateway that does describe its models is read in `_facts`, and only as it says.)
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterator

from ..errors import ProviderError, Unsupported
from ..types import CheckResult, Discovered, Finished, TextDelta, ToolUse, Usage, Target
from . import _wire as wire

FORMAT = "openai-chat"

_FINISH = {"stop": "stop", "tool_calls": "tool_calls", "function_call": "tool_calls",
           "length": "length", "content_filter": "content_filter"}


def _auth(target: Target) -> dict[str, str]:
    return {"Authorization": f"Bearer {target.api_key}"} if target.api_key else {}


def _listing(target: Target) -> list[dict[str, Any]]:
    body = wire.get_json(wire.join_url(target.base_url, "models"), headers=_auth(target), missing="address")
    rows = body.get("data") if isinstance(body, dict) else body
    if not isinstance(rows, list):
        raise ProviderError(f"{wire.host_of(target.base_url)} answered, but not with a list of models.",
                            kind="request")
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
        status, words = wire.probe_post(wire.join_url(target.base_url, "chat/completions"),
                                        headers=_auth(target), body={})
        if status in (401, 403, 404) or status >= 500:
            raise wire.error_for(status, words, target.base_url, missing="address")
        return CheckResult(True, "Reached the server. It doesn't offer a list of its models, so the "
                                 "key and the model can't be checked until you use it — add a model ID by hand.")
    return CheckResult(True, f"Connected. {count} model{'s' if count != 1 else ''} available.")


def discover(target: Target) -> list[Discovered]:
    try:
        rows = _listing(target)
    except ProviderError as err:
        if err.status == 404:
            raise Unsupported("This server doesn't offer a list of its models — add the model ID by hand.") from err
        raise
    return [Discovered(model_id=str(r["id"]), facts=_facts(r)) for r in rows]


#: Kinds of model a gateway can list that never take a chat turn.
_NOT_CHAT_TYPES = {"video", "image", "audio", "speech", "tts", "stt", "transcription",
                   "embedding", "embeddings", "rerank", "moderation"}


def _facts(row: dict[str, Any]) -> dict[str, Any] | None:
    """What a gateway said about this model, and only that.

    Plain OpenAI-style servers say nothing beyond the name, and get nothing here.
    Gateways such as OmniRoute and OpenRouter do say more — a `type`, the
    modalities in and out, whether tool calling works — and those are kept as
    reported. A key that is absent means the provider did not say.
    """
    facts: dict[str, Any] = {}
    # OmniRoute marks its own routing/combo entries this way — confirmed live, not
    # documented. It is the only signal this kind of gateway reports anywhere Jarvis can
    # reach that tells a router apart from a model pinned to one upstream provider. Read
    # as reported, same as everything here — never guessed from the model id itself.
    if row.get("owned_by") == "combo":
        facts["router"] = True
    arch = row.get("architecture") if isinstance(row.get("architecture"), dict) else {}
    outputs = row.get("output_modalities") or arch.get("output_modalities")
    inputs = row.get("input_modalities") or arch.get("input_modalities")
    kind = row.get("type")
    if (isinstance(kind, str) and kind.lower() in _NOT_CHAT_TYPES) or (
            isinstance(outputs, list) and outputs and "text" not in outputs):
        facts["chat"] = False
    caps = row.get("capabilities") if isinstance(row.get("capabilities"), dict) else {}
    supported = row.get("supported_parameters")
    if isinstance(caps.get("tool_calling"), bool):
        facts["tools"] = caps["tool_calling"]
    elif isinstance(supported, list) and supported:
        facts["tools"] = "tools" in supported
    if isinstance(inputs, list) and inputs:
        facts["image"] = "image" in inputs
    # A price, where the gateway states one. Kept because "needs credit" is a fact about
    # paid models only; a model it lists as free keeps working on an account with none.
    pricing = row.get("pricing") if isinstance(row.get("pricing"), dict) else {}
    # OpenRouter's own router products (Auto Router, Pareto Router, Fusion, Body Builder) mark
    # themselves this way — confirmed live, not documented either. What they cost depends on
    # which underlying model answers, so the listing can't quote one; a plain rolling alias to
    # one current model (e.g. "~anthropic/claude-sonnet-latest") still prices normally and is
    # correctly left alone. `openrouter/free` prices at a real 0, not -1, so it isn't caught
    # here — disclosed, not missed: it behaves as an ordinary model when tried.
    if pricing.get("prompt") == "-1":
        facts["router"] = True
    try:
        prices = [float(pricing[k]) for k in ("prompt", "completion") if k in pricing]
    except (TypeError, ValueError):
        prices = []
    if prices:
        facts["free"] = all(p == 0 for p in prices)
    return facts or None


# --- the conversation, in this format ------------------------------------------------

def _user(message: dict[str, Any]) -> dict[str, Any] | None:
    text = message.get("text") or ""
    media = wire.media_of(message)
    if not media:
        return {"role": "user", "content": text} if text else None
    parts: list[dict[str, Any]] = [{"type": "text", "text": text}] if text else []
    for kind, mime, data in media:
        if kind != "image":
            raise ProviderError(f"This kind of connection can take images, but not {kind} attachments.",
                                kind="request")
        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}})
    return {"role": "user", "content": parts}


def _assistant(message: dict[str, Any]) -> dict[str, Any] | None:
    text = wire.assistant_text(message)
    calls = message.get("toolCalls") or []
    if not text and not calls:
        return None
    entry: dict[str, Any] = {"role": "assistant", "content": text or None}
    if calls:
        entry["tool_calls"] = [
            {"id": c["id"], "type": "function",
             "function": {"name": c["name"], "arguments": wire.dumps(c.get("args") or {})}}
            for c in calls
        ]
    return entry


def _messages(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    flat = wire.flatten_system(system)
    if flat:
        out.append({"role": "system", "content": flat})
    for message in messages:
        role = message.get("role")
        if role == "user":
            entry = _user(message)
        elif role == "assistant":
            entry = _assistant(message)
        elif role == "tool":
            out.extend({"role": "tool", "tool_call_id": r["id"], "content": wire.dumps(r.get("result"))}
                       for r in message.get("toolResults") or [])
            continue
        else:
            continue
        if entry:
            out.append(entry)
    return out


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _usage(raw: dict[str, Any]) -> Usage:
    return Usage(
        tokens_in=_count(raw.get("prompt_tokens")),
        tokens_out=_count(raw.get("completion_tokens")),
        tokens_reasoning=_count((raw.get("completion_tokens_details") or {}).get("reasoning_tokens")),
        cached_in=_count((raw.get("prompt_tokens_details") or {}).get("cached_tokens")),
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


def _error_in_text(text: str, host: str) -> ProviderError | None:
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
    kind = "rate" if "rate" in str(err.get("type") or err.get("code") or "") else "server"
    return ProviderError(f"{host} sent back an error instead of a reply: {message.strip()}",
                         kind=kind)


def stream(target: Target, *, model_id: str, messages: list[dict[str, Any]], system: str,
           tools: list[dict[str, Any]], effort: str | None = None,
           facts: dict[str, Any] | None = None) -> Iterator[Any]:
    """Nothing is done with `effort`: this format has no reasoning control, so a
    server of this kind is never sent one."""
    host = wire.host_of(target.base_url)
    body: dict[str, Any] = {
        "model": model_id,
        "messages": _messages(system, messages),
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if tools:
        body["tools"] = [{"type": "function",
                          "function": {"name": t["name"], "description": t.get("description", ""),
                                       "parameters": t.get("parameters") or {"type": "object", "properties": {}}}}
                         for t in tools]

    text: list[str] = []
    calls: dict[int, dict[str, str | None]] = {}
    finish: str | None = None
    finished_cleanly = False
    usage: Usage | None = None
    reported: str | None = None
    # Text held back while it could still be a gateway's error envelope (see above).
    held: list[str] = []
    holding = True

    with wire.post_stream(wire.join_url(target.base_url, "chat/completions"),
                          headers=_auth(target), body=body) as response:
        for _, data in wire.iter_sse(response):
            if data.strip() == "[DONE]":
                finished_cleanly = True
                break
            chunk = wire.loads_event(data, host)
            if not isinstance(chunk, dict):
                continue
            if chunk.get("error"):
                err = chunk["error"]
                raise ProviderError(f"{host} stopped the reply: "
                                    f"{err.get('message') if isinstance(err, dict) else err}", kind="server")
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
        failure = _error_in_text("".join(held), host)
        if failure is not None:
            raise failure
        yield TextDelta("".join(held))

    if finish is None and not finished_cleanly:
        # A reply that just stops is not a finished reply. Saying so is the
        # difference between "it broke" and a confident half-answer.
        raise ProviderError(f"The reply from {host} stopped part-way.", kind="reply")

    tool_calls = []
    for index in sorted(calls):
        slot = calls[index]
        if not slot["name"]:
            raise ProviderError("The model asked to use a tool without naming it, so nothing was run.",
                                kind="reply")
        tool_calls.append(ToolUse(id=slot["id"] or f"call_{index}", name=slot["name"],
                                  args=wire.parse_arguments(slot["args"], slot["name"])))

    reason = "tool_calls" if tool_calls else _FINISH.get(finish or "stop", "stop")
    yield Finished(text="".join(text), tool_calls=tuple(tool_calls), finish_reason=reason,
                   usage=usage, model_id=reported)
