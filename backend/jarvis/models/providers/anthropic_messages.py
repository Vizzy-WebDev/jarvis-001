"""Anthropic's own API — the Messages endpoint.

The one provider that says, per model, which reasoning-effort levels it accepts
and how large a reply it will produce. Both come straight from its model list
into `Discovered.facts`, and both are used only as reported.

Two things about Claude's newest models shape this module:

* Thinking is on by default. Its text is omitted, but every reply carries
  thinking blocks with a `signature`, and a tool-use loop must hand them back
  "complete and unmodified" or the request is refused. So a reply is kept
  verbatim in `raw` and replayed as-is inside a loop. Finished turns' thinking is
  simply left out (the API allows it), which also means a signature from one
  model is never sent to another.
* `max_tokens` is required, and thinking counts against it.
"""

from __future__ import annotations

from typing import Any, Iterator

from ...prompt_format import CACHE_BREAK
from ..errors import ProviderError
from ..types import CheckResult, Discovered, Finished, TextDelta, ToolUse, Usage, Target
from . import _wire as wire

FORMAT = "anthropic-messages"
VERSION = "2023-06-01"

#: What to ask for when the model's own ceiling was reported, capped here — a
#: request at high effort wants room to think as well as answer.
PREFERRED_MAX_TOKENS = 64_000
#: When the model's ceiling isn't known (a model added by hand), stay well inside
#: what any current model accepts rather than have the request refused outright.
FALLBACK_MAX_TOKENS = 16_384

_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
#: The API's own default, from its documentation: "high" is the same as omitting.
_EFFORT_DEFAULT = "high"

_STOP = {"end_turn": "stop", "stop_sequence": "stop", "pause_turn": "stop", "tool_use": "tool_calls",
         "max_tokens": "length", "model_context_window_exceeded": "length", "refusal": "content_filter"}


def _auth(target: Target) -> dict[str, str]:
    headers = {"anthropic-version": VERSION}
    if target.api_key:
        headers["x-api-key"] = target.api_key
    return headers


def check(target: Target) -> CheckResult:
    wire.get_json(wire.join_url(target.base_url, "models"), headers=_auth(target),
                  params={"limit": 1}, missing="address")
    return CheckResult(True, "Connected to Anthropic.")


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _discovered(model: dict[str, Any]) -> Discovered:
    facts: dict[str, Any] = {}
    ceiling = _count(model.get("max_tokens"))
    if ceiling:  # 0 or missing is "not reported", not "zero"
        facts["maxOutput"] = ceiling
    effort = (model.get("capabilities") or {}).get("effort") or {}
    levels = [level for level in _EFFORT_LEVELS if (effort.get(level) or {}).get("supported")]
    if effort.get("supported") and levels:
        facts["effort"] = {"levels": levels, "default": _EFFORT_DEFAULT if _EFFORT_DEFAULT in levels else None}
    return Discovered(model_id=str(model["id"]), label=model.get("display_name") or None, facts=facts or None)


def discover(target: Target) -> list[Discovered]:
    found: list[Discovered] = []
    after: str | None = None
    for _ in range(20):  # a bound, so a provider that never says "no more" can't loop forever
        params: dict[str, Any] = {"limit": 1000}
        if after:
            params["after_id"] = after
        body = wire.get_json(wire.join_url(target.base_url, "models"), headers=_auth(target),
                             params=params, missing="address")
        rows = body.get("data") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            raise ProviderError("Anthropic answered, but not with a list of models.", kind="request")
        found.extend(_discovered(m) for m in rows if isinstance(m, dict) and m.get("id"))
        if not (isinstance(body, dict) and body.get("has_more")):
            break
        after = body.get("last_id")
        if not after:
            break
    return found


# --- the conversation, in this format ------------------------------------------------

def _system(system: str) -> list[dict[str, Any]]:
    """Two blocks, with the cache breakpoint on the stable one: what stays the
    same every turn is cached, and what changes every turn (the time, this turn's
    memories) comes after it, where it can't invalidate the cached prefix."""
    stable, _, volatile = (system or "").partition(CACHE_BREAK)
    blocks: list[dict[str, Any]] = []
    if stable.strip():
        blocks.append({"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}})
    if volatile.strip():
        blocks.append({"type": "text", "text": volatile})
    return blocks


def _user_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if message.get("text"):
        blocks.append({"type": "text", "text": message["text"]})
    for kind, mime, data in wire.media_of(message):
        if kind != "image":
            raise ProviderError(f"Anthropic can take images here, but not {kind} attachments.", kind="request")
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}})
    return blocks


def _assistant_blocks(message: dict[str, Any], keep_thinking: bool) -> list[dict[str, Any]]:
    raw = message.get("raw")
    if (isinstance(raw, dict) and raw.get("adapter") == FORMAT and isinstance(raw.get("content"), list)
            and not message.get("interrupted")):
        blocks = [dict(b) for b in raw["content"] if isinstance(b, dict)]
        if not keep_thinking:
            blocks = [b for b in blocks if b.get("type") not in ("thinking", "redacted_thinking")]
        # The API refuses an empty text block, which a verbatim replay can contain.
        return [b for b in blocks if not (b.get("type") == "text" and not b.get("text"))]
    blocks = []
    text = wire.assistant_text(message)
    if text:
        blocks.append({"type": "text", "text": text})
    for call in message.get("toolCalls") or []:
        blocks.append({"type": "tool_use", "id": call["id"], "name": call["name"], "input": call.get("args") or {}})
    return blocks


def _messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Inside the tool-use loop that is still running — everything after the last
    # thing the person said — thinking blocks must go back untouched. Before it,
    # they may be left out.
    last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    out: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        role = message.get("role")
        if role == "user":
            side, blocks = "user", _user_blocks(message)
        elif role == "assistant":
            side, blocks = "assistant", _assistant_blocks(message, keep_thinking=index > last_user)
        elif role == "tool":
            side = "user"
            blocks = []
            for result in message.get("toolResults") or []:
                block: dict[str, Any] = {"type": "tool_result", "tool_use_id": result["id"],
                                         "content": wire.dumps(result.get("result"))}
                if wire.is_error_result(result.get("result")):
                    block["is_error"] = True
                blocks.append(block)
        else:
            continue
        if not blocks:
            continue
        if out and out[-1]["role"] == side:
            out[-1]["content"].extend(blocks)  # the API wants alternating turns
        else:
            out.append({"role": side, "content": blocks})

    # It must open with the person speaking. A trimmed window can start on a reply
    # or on a tool result whose call was trimmed away; either would be refused.
    while out and (out[0]["role"] != "user"
                   or all(b.get("type") == "tool_result" for b in out[0]["content"])):
        out.pop(0)
    return out


def _levels(facts: dict[str, Any] | None) -> list[str]:
    return list(((facts or {}).get("effort") or {}).get("levels") or [])


def stream(target: Target, *, model_id: str, messages: list[dict[str, Any]], system: str,
           tools: list[dict[str, Any]], effort: str | None = None,
           facts: dict[str, Any] | None = None) -> Iterator[Any]:
    ceiling = _count((facts or {}).get("maxOutput"))
    body: dict[str, Any] = {
        "model": model_id,
        "max_tokens": min(ceiling, PREFERRED_MAX_TOKENS) if ceiling else FALLBACK_MAX_TOKENS,
        "messages": _messages(messages),
        "stream": True,
    }
    system_blocks = _system(system)
    if system_blocks:
        body["system"] = system_blocks
    if tools:
        body["tools"] = [{"name": t["name"], "description": t.get("description", ""),
                          "input_schema": t.get("parameters") or {"type": "object", "properties": {}}}
                         for t in tools]
    # Effort travels alone: the API accepts it with or without thinking, so no
    # thinking mode is forced on. Only a level this model was REPORTED to accept
    # is ever sent — never one merely remembered from the last model.
    if effort and effort in _levels(facts):
        body["output_config"] = {"effort": effort}

    blocks: dict[int, dict[str, Any]] = {}
    json_parts: dict[int, list[str]] = {}
    reported: str | None = None
    stop_reason: str | None = None
    usage: dict[str, Any] = {}
    finished = False

    with wire.post_stream(wire.join_url(target.base_url, "messages"), headers=_auth(target), body=body) as response:
        for event, data in wire.iter_sse(response):
            payload = wire.loads_event(data, "Anthropic")
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
                if block.get("type") == "tool_use":
                    json_parts[index] = []
                    block["input"] = {}
                blocks[index] = block
            elif kind == "content_block_delta":
                index, delta = payload["index"], payload.get("delta") or {}
                block = blocks.setdefault(index, {})
                dtype = delta.get("type")
                if dtype == "text_delta":
                    piece = delta.get("text") or ""
                    block["text"] = block.get("text", "") + piece
                    if piece:
                        yield TextDelta(piece)
                elif dtype == "input_json_delta":
                    json_parts.setdefault(index, []).append(delta.get("partial_json") or "")
                elif dtype == "thinking_delta":
                    block["thinking"] = block.get("thinking", "") + (delta.get("thinking") or "")
                elif dtype == "signature_delta":
                    block["signature"] = block.get("signature", "") + (delta.get("signature") or "")
            elif kind == "content_block_stop":
                index = payload["index"]
                block = blocks.get(index) or {}
                if index in json_parts:
                    block["input"] = wire.parse_arguments("".join(json_parts[index]), block.get("name") or "a tool")
            elif kind == "message_delta":
                stop_reason = (payload.get("delta") or {}).get("stop_reason") or stop_reason
                usage.update({k: v for k, v in (payload.get("usage") or {}).items() if v is not None})
            elif kind == "message_stop":
                finished = True
                break
            elif kind == "error":
                err = payload.get("error") or {}
                raise ProviderError(f"Anthropic stopped the reply. {err.get('message') or ''}".strip(),
                                    kind="server")

    if not finished:
        raise ProviderError("The reply from Anthropic stopped part-way.", kind="reply")

    content = [blocks[i] for i in sorted(blocks)]
    tool_calls = tuple(ToolUse(id=b["id"], name=b["name"], args=b.get("input") or {})
                       for b in content if b.get("type") == "tool_use")
    text = "".join(b.get("text", "") for b in content if b.get("type") == "text")

    reading = _count(usage.get("cache_read_input_tokens"))
    creating = _count(usage.get("cache_creation_input_tokens")) or 0
    fresh = _count(usage.get("input_tokens"))
    yield Finished(
        text=text,
        tool_calls=tool_calls,
        finish_reason="tool_calls" if tool_calls else _STOP.get(stop_reason or "end_turn", "stop"),
        usage=Usage(
            tokens_in=None if fresh is None else fresh + creating + (reading or 0),
            tokens_out=_count(usage.get("output_tokens")),
            tokens_reasoning=_count((usage.get("output_tokens_details") or {}).get("thinking_tokens")),
            cached_in=reading,
        ),
        model_id=reported,
        raw={"adapter": FORMAT, "content": content},
    )
