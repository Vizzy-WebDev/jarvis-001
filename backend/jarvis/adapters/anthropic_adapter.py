"""Anthropic (Claude) — the Messages API wire format.

Two details carried over deliberately:

* **The raw round-trip.** An assistant turn's own content blocks are replayed
  verbatim when they exist, rather than rebuilt from our neutral fields —
  rebuilding loses information the provider expects back unchanged. The one
  exception is an INTERRUPTED turn, where the raw content is exactly what must
  NOT be replayed: it holds everything generated after the user cut in. Losing
  raw fidelity for that one turn is the correct trade — an interrupted turn is
  already an edited turn.

* **A cache breakpoint between the stable and volatile halves of the system
  prompt.** Everything up to the breakpoint is reused across turns; the volatile
  half rides after it with no `cache_control` of its own, so it never has to
  match byte-for-byte for the cached prefix to still hit.
"""

from __future__ import annotations

import json
from typing import Any, Iterator

from ..config import get_secret
from ..conversation import assistant_text_of
from ..orchestrator.model_port import ModelEvent, StepComplete, TextChunk, ToolCall
from ..prompt_format import CACHE_BREAK
from .base import AdapterError

name = "anthropic"

MAX_TOKENS = 4096
REQUEST_TIMEOUT_S = 120.0
CAPABILITIES = {"video": False, "audio": False, "vision": True, "webSearch": False}



class NoApiKey(AdapterError):
    pass


def _key(entry: dict[str, Any]) -> str | None:
    if "secretValue" in entry:
        return entry["secretValue"]
    ref = entry.get("secretRef")
    return get_secret(ref) if ref else None


def _client(entry: dict[str, Any]):
    from anthropic import Anthropic

    key = _key(entry)
    if not key:
        raise NoApiKey("No API key configured.")
    # baseUrl is set only for a Custom connection whose probe resolved to this
    # wire shape against a non-Anthropic host. A real Anthropic account has none.
    # max_retries=0: retry policy belongs to the gateway, not to each SDK —
    # see the same note in openai_compatible.py.
    return Anthropic(api_key=key, base_url=entry.get("baseUrl") or None,
                     max_retries=0, timeout=REQUEST_TIMEOUT_S)


def _media_blocks(media: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    blocks = []
    for item in media or []:
        if item.get("kind") not in (None, "image") or not item.get("dataBase64"):
            continue
        blocks.append({"type": "image", "source": {
            "type": "base64", "media_type": item.get("mimeType", "image/png"),
            "data": item["dataBase64"]}})
    return blocks


def to_wire(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role == "user" and m.get("media"):
            content = ([{"type": "text", "text": m["text"]}] if m.get("text") else [])
            out.append({"role": "user", "content": content + _media_blocks(m.get("media"))})
        elif role == "user" and m.get("text"):
            out.append({"role": "user", "content": m["text"]})
        elif role == "assistant":
            spoken = assistant_text_of(m)
            raw = m.get("raw") or {}
            if not m.get("interrupted") and raw.get("adapter") == "anthropic" and raw.get("content"):
                out.append({"role": "assistant", "content": raw["content"]})
            elif m.get("toolCalls"):
                content = ([{"type": "text", "text": spoken}] if spoken else [])
                content += [{"type": "tool_use", "id": c.get("id"), "name": c.get("name"),
                             "input": c.get("args") or {}} for c in m["toolCalls"]]
                out.append({"role": "assistant", "content": content})
            elif spoken:
                out.append({"role": "assistant", "content": spoken})
        elif role == "tool" and m.get("toolResults"):
            out.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": r.get("id"),
                 "content": json.dumps(r.get("result"), default=str)}
                for r in m["toolResults"]]})
    return out


def _system_blocks(system: str) -> list[dict[str, Any]]:
    stable, _, volatile = system.partition(CACHE_BREAK)
    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}}]
    if volatile:
        blocks.append({"type": "text", "text": volatile})
    return blocks


def _tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [{"name": t["name"], "description": t.get("description", ""),
             "input_schema": t.get("parameters") or {"type": "object", "properties": {}}}
            for t in tools or []]


def stream(
    entry: dict[str, Any],
    messages: list[dict[str, Any]],
    *,
    system: str = "",
    tools: list[dict[str, Any]] | None = None,
) -> Iterator[ModelEvent]:
    client = _client(entry)
    with client.messages.stream(
        model=entry["model"],
        max_tokens=MAX_TOKENS,
        system=_system_blocks(system),
        messages=to_wire(messages),
        tools=_tools(tools),
    ) as running:
        for event in running:
            if (getattr(event, "type", None) == "content_block_delta"
                    and getattr(event.delta, "type", None) == "text_delta"):
                yield TextChunk(event.delta.text)
        message = running.get_final_message()

    blocks = [b.model_dump() if hasattr(b, "model_dump") else dict(b) for b in message.content]
    uses = [b for b in blocks if b.get("type") == "tool_use"]
    raw = {"adapter": "anthropic", "content": blocks}

    if message.stop_reason == "tool_use" and uses:
        yield StepComplete(
            text="".join(b.get("text", "") for b in blocks if b.get("type") == "text"),
            tool_calls=tuple(ToolCall(b["id"], b["name"], b.get("input") or {}) for b in uses),
            model_id=entry.get("id"), raw=raw,
        )
        return

    # Join ALL text blocks: a reply can carry more than one, and taking only the
    # first showed the full reply live while persisting its opening fragment.
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    if not text:
        raise AdapterError("The model returned an empty response — try again later.")
    yield StepComplete(text=text, model_id=entry.get("id"), raw=raw)


def test_connection(entry: dict[str, Any]) -> dict[str, Any]:
    try:
        client = _client(entry)
        message = client.messages.create(
            model=entry["model"], max_tokens=32,
            messages=[{"role": "user", "content": 'Say "ready" and nothing else.'}])
        text = "".join(getattr(b, "text", "") for b in message.content)
        if not text:
            return {"ok": False, "error": "The server responded but with no text — check the model name."}
        return {"ok": True}
    except NoApiKey:
        return {"ok": False, "error": "That connection needs an API key.", "friendly": True}
    except Exception as err:  # noqa: BLE001
        return {"ok": False, "error": friendly_error(err)}


def list_models(entry: dict[str, Any]) -> list[dict[str, Any]]:
    client = _client(entry)
    return [{"model": m.id, "label": getattr(m, "display_name", None)}
            for m in client.models.list()]


def friendly_error(err: BaseException) -> str:
    body = getattr(err, "body", None)
    if isinstance(body, dict):
        inner = body.get("error")
        if isinstance(inner, dict) and isinstance(inner.get("message"), str):
            return inner["message"]
    return str(err) or "That connection didn't work."
