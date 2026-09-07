"""The "any OpenAI-shaped server" adapter.

OpenAI itself, Ollama, LM Studio, OpenRouter, Groq, Together — all of them speak
this wire format, so all of them work by pointing `baseUrl` elsewhere with no new
code per provider.

Two behaviours here are not obvious and are ported deliberately, because each was
found live rather than designed:

* **A 200 OK carrying an error payload as ordinary content.** A gateway whose own
  upstream pool had failed returned `{"error":{"message":"[429] ... Rate limit
  exceeded"}}` as the assistant's message body. Undetected, that text streams to
  the transcript and is spoken aloud as a real reply, and the turn is recorded as
  a success. So a reply that STARTS with `{` is held back briefly and inspected;
  anything that is not that exact shape streams normally from then on.

* **An empty final response is a failure, not an empty success.** Yielding a
  placeholder would mark a broken model healthy and show the user nothing.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterator

from ..config import get_secret
from ..conversation import assistant_text_of
from ..orchestrator.model_port import ModelEvent, StepComplete, TextChunk, ToolCall
from . import usage as usage_read
from .base import AdapterError

name = "openai-compatible"

#: Hosts known to actually enforce a key. Anything else with a custom baseUrl is
#: assumed keyless — but only when the connection has no stored `keyRequired`
#: fact, which is always more accurate than this guess.
KEY_REQUIRED_HOSTS = re.compile(r"openai\.com|openrouter\.ai|groq\.com|together\.(ai|xyz)", re.I)

#: The ceiling of this wire format — a seed for a model's own caps, not a gate.
#: `webSearch` is false because it is not wired up here; claiming a capability
#: that is not implemented is worse than not having it (§45).
CAPABILITIES = {"video": False, "audio": False, "vision": True, "webSearch": False}

#: A provider that never answers must not hang the turn forever.
REQUEST_TIMEOUT_S = 120.0

#: How much of a reply that starts with '{' is held before deciding it is not an
#: error blob. Real ones seen in practice are far shorter than this.
ERROR_PAYLOAD_HOLD_CHARS = 500


class NoApiKey(AdapterError):
    pass


def _key(entry: dict[str, Any]) -> str | None:
    if "secretValue" in entry:
        return entry["secretValue"]
    ref = entry.get("secretRef")
    return get_secret(ref) if ref else None


def _require_key(entry: dict[str, Any]) -> None:
    stored = entry.get("keyRequired")
    if isinstance(stored, bool):
        needs = stored
    else:
        base = entry.get("baseUrl")
        needs = not base or bool(KEY_REQUIRED_HOSTS.search(base))
    if needs and not _key(entry):
        raise NoApiKey("No API key configured.")


def _client(entry: dict[str, Any]):
    from openai import OpenAI  # imported lazily: a missing SDK is a startup

    # max_retries=0 on purpose. The SDK retries 429s and 5xxs by default, which
    # would silently swallow exactly the failures the gateway needs to see in
    # order to bench a model and move to the next candidate — a hidden retry
    # costs the user latency on a model already known to be out of quota.
    # Retry policy belongs to the gateway, in one place.
    return OpenAI(api_key=_key(entry) or "not-needed", base_url=entry.get("baseUrl") or None,
                  max_retries=0, timeout=REQUEST_TIMEOUT_S)


def _error_payload_message(text: str) -> str | None:
    trimmed = (text or "").strip()
    if not trimmed.startswith("{"):
        return None
    try:
        parsed = json.loads(trimmed)
    except ValueError:
        return None
    message = (parsed or {}).get("error", {})
    message = message.get("message") if isinstance(message, dict) else None
    return message if isinstance(message, str) and message else None


def _media_parts(media: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Images only — this format has no video part type, so video is dropped
    rather than mislabelled as something it is not."""
    parts = []
    for item in media or []:
        if item.get("kind") not in (None, "image"):
            continue
        url = (f"data:{item.get('mimeType')};base64,{item['dataBase64']}"
               if item.get("dataBase64") else item.get("uri"))
        if url:
            parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts


def to_wire(messages: list[dict[str, Any]], system: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in messages:
        role = m.get("role")
        if role == "user" and m.get("media"):
            content = ([{"type": "text", "text": m["text"]}] if m.get("text") else [])
            out.append({"role": "user", "content": content + _media_parts(m.get("media"))})
        elif role == "user" and m.get("text"):
            out.append({"role": "user", "content": m["text"]})
        elif role == "assistant":
            # What the user actually HEARD, not everything that was generated
            # after a barge-in cut the reply off.
            spoken = assistant_text_of(m)
            if m.get("toolCalls"):
                out.append({
                    "role": "assistant",
                    "content": spoken or None,
                    "tool_calls": [
                        {"id": c.get("id"), "type": "function",
                         "function": {"name": c.get("name"),
                                      "arguments": json.dumps(c.get("args") or {})}}
                        for c in m["toolCalls"]
                    ],
                })
            elif spoken:
                out.append({"role": "assistant", "content": spoken})
        elif role == "tool" and m.get("toolResults"):
            for r in m["toolResults"]:
                out.append({"role": "tool", "tool_call_id": r.get("id"),
                            "content": json.dumps(r.get("result"), default=str)})
    return out


def _tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    return [{"type": "function",
             "function": {"name": t["name"], "description": t.get("description", ""),
                          "parameters": t.get("parameters") or {"type": "object", "properties": {}}}}
            for t in tools]


def stream(
    entry: dict[str, Any],
    messages: list[dict[str, Any]],
    *,
    system: str = "",
    tools: list[dict[str, Any]] | None = None,
) -> Iterator[ModelEvent]:
    _require_key(entry)
    client = _client(entry)

    response = client.chat.completions.create(
        model=entry["model"],
        messages=to_wire(messages, system),
        tools=_tools(tools),
        stream=True,
        # Without this an OpenAI-shaped stream never sends usage at all — it is
        # not discarded, it is never requested. A backend that does not know the
        # option ignores it, and usage simply stays absent; never fabricated.
        stream_options={"include_usage": True},
    )

    text = ""
    yielded = 0
    holding = False
    hold_decided = False
    calls: dict[int, dict[str, str]] = {}
    usage: dict[str, int] | None = None

    def release() -> Iterator[ModelEvent]:
        nonlocal yielded
        pending = text[yielded:]
        yielded = len(text)
        if pending:
            yield TextChunk(pending)

    for chunk in response:
        # The usage-only final chunk carries no choices. It used to be skipped
        # outright, which is why nothing in this build knew what a turn cost.
        usage = usage_read.from_openai(getattr(chunk, "usage", None)) or usage

        choices = getattr(chunk, "choices", None) or []
        if not choices:
            continue
        delta = choices[0].delta
        if delta is None:
            continue

        if getattr(delta, "content", None):
            text += delta.content
            if not hold_decided:
                if text.lstrip().startswith("{"):
                    holding = True
                elif text.strip():
                    hold_decided = True   # first real character isn't '{'
            if holding and len(text) - yielded >= ERROR_PAYLOAD_HOLD_CHARS:
                holding, hold_decided = False, True
            if not holding:
                yield from release()

        for tc in getattr(delta, "tool_calls", None) or []:
            # A real tool call proves this was never an error payload.
            if holding:
                holding, hold_decided = False, True
                yield from release()
            slot = calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
            if tc.id:
                slot["id"] = tc.id
            if tc.function and tc.function.name:
                slot["name"] += tc.function.name
            if tc.function and tc.function.arguments:
                slot["args"] += tc.function.arguments

    if holding:
        message = _error_payload_message(text)
        if message:
            raise AdapterError(message)
        yield from release()

    if calls:
        parsed = []
        for slot in calls.values():
            try:
                args = json.loads(slot["args"] or "{}")
            except ValueError:
                args = {}            # a malformed call is refused downstream, not crashed on
            parsed.append(ToolCall(slot["id"], slot["name"], args if isinstance(args, dict) else {}))
        yield StepComplete(text=text, tool_calls=tuple(parsed), model_id=entry.get("id"),
                           usage=usage)
        return

    if not text:
        raise AdapterError("The model returned an empty response — try again later.")

    yield StepComplete(text=text, model_id=entry.get("id"), usage=usage)


def test_connection(entry: dict[str, Any]) -> dict[str, Any]:
    """Prove GENERATION, not just that the address answers (§26).

    Listing models proves an endpoint exists and a key is accepted for listing.
    It does not prove this model can produce a token — the exact gap that let a
    gateway connection save as keyless and then 401 on every real turn.
    """
    try:
        _require_key(entry)
        client = _client(entry)
        response = client.chat.completions.create(
            model=entry["model"],
            messages=[{"role": "user", "content": 'Say "ready" and nothing else.'}],
        )
        text = response.choices[0].message.content if response.choices else None
        if not text:
            return {"ok": False, "error": "The server responded but with no text — check the model name."}
        return {"ok": True}
    except NoApiKey:
        return {"ok": False, "error": "That server needs an API key.", "friendly": True}
    except Exception as err:  # noqa: BLE001
        status = getattr(err, "status_code", None) or getattr(err, "status", None)
        if status == 401 and not _key(entry):
            # Reached it, needs a key — never "that key is invalid" for a key
            # the user was never asked for.
            return {"ok": False, "error": "That server was reached but needs an API key.",
                    "friendly": True}
        return {"ok": False, "error": friendly_error(err)}


def list_models(entry: dict[str, Any]) -> list[dict[str, Any]]:
    _require_key(entry)
    client = _client(entry)
    out = []
    for model in client.models.list():
        out.append({"model": model.id,
                    "contextTokens": getattr(model, "context_length", None)})
    return out


def friendly_error(err: BaseException) -> str:
    """The human-readable half. SDK `.message` is raw JSON on every provider."""
    for attr_chain in (("body", "error", "message"), ("error", "message")):
        node: Any = err
        for attr in attr_chain:
            node = node.get(attr) if isinstance(node, dict) else getattr(node, attr, None)
            if node is None:
                break
        if isinstance(node, str) and node:
            return node
    message = str(err)
    if "Connection error" in message or "connect" in message.lower():
        return "Couldn't reach that address — is the server running?"
    return message or "That connection didn't work."
