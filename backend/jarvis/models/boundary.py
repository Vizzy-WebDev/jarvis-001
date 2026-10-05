"""Between the app and the layer: the stored conversation format in, canonical
items out; a response back into what the app stores and reports.

Shared by `client.py` (the turn loop's port) and `oneshot.py` (`ai.ask`). Imports
nothing that reaches the turn loop, so a tool asking a question through `ai.py`
is never led into it.

The conversation store keeps each assistant message's canonical output items —
text, tool calls with their canonical ids, and sealed provider state — in its
`raw` field (`{"layer": 1, "items": [...]}`), so the next request carries them
back unchanged. An interrupted message is rebuilt from what was actually said,
without its provider state.
"""

from __future__ import annotations

import logging
from typing import Any

from ..ai import NoModelAvailable
from ..conversation import assistant_text_of
from ..events import EventType, bus
from . import config
from .catalog import split_endpoint_id
from .errors import ModelError
from .types import (ImagePart, Item, Message, Response, Sealed, TextPart, ToolCall, ToolResult)

logger = logging.getLogger(__name__)

RAW_VERSION = 1


# --- stored items ----------------------------------------------------------------------------

def item_to_json(item: Item) -> dict[str, Any]:
    if isinstance(item, Message):
        return {"type": "message", "role": item.role,
                "parts": [{"text": p.text} if isinstance(p, TextPart) else {"image": p.mime, "data": p.data_b64}
                          for p in item.parts]}
    if isinstance(item, ToolCall):
        return {"type": "tool_call", "id": item.id, "name": item.name,
                "arguments": dict(item.arguments) if item.arguments is not None else None,
                "raw_arguments": item.raw_arguments}
    if isinstance(item, ToolResult):
        return {"type": "tool_result", "call_id": item.call_id, "name": item.name, "content": item.content,
                "is_error": item.is_error}
    return {"type": "sealed", "endpoint": item.endpoint_id, "kind": item.kind, "payload": item.payload}


def item_from_json(row: dict[str, Any]) -> Item | None:
    kind = row.get("type")
    if kind == "message":
        parts = tuple(TextPart(p["text"]) if "text" in p else ImagePart(p["image"], p["data"])
                      for p in row.get("parts") or [])
        return Message(row.get("role", "assistant"), parts)
    if kind == "tool_call":
        return ToolCall(row["id"], row["name"], row.get("arguments"), row.get("raw_arguments"))
    if kind == "tool_result":
        return ToolResult(row["call_id"], row.get("name", ""), row.get("content"), bool(row.get("is_error")))
    if kind == "sealed":
        return Sealed(row["endpoint"], row["kind"], row.get("payload"))
    return None


def raw_for(response: Response) -> dict[str, Any]:
    """What the conversation store keeps with an assistant message."""
    return {"layer": RAW_VERSION, "items": [item_to_json(i) for i in response.items]}


def _media(message: dict[str, Any]) -> list[ImagePart]:
    parts = []
    for media in message.get("media") or []:
        data = media.get("dataBase64")
        if not data:
            continue
        kind = str(media.get("kind") or "image")
        if kind != "image":
            raise NoModelAvailable(f"Jarvis can send pictures to a model, but not {kind} attachments yet.",
                                   detail={"reason": "unsupported_attachment", "kind": kind})
        parts.append(ImagePart(str(media.get("mimeType") or "image/png"), data))
    return parts


def items_from_conversation(messages: list[dict[str, Any]]) -> tuple[Item, ...]:
    out: list[Item] = []
    for message in messages:
        role = message.get("role")
        if role == "user":
            parts: list[Any] = [TextPart(message["text"])] if message.get("text") else []
            parts += _media(message)
            if parts:
                out.append(Message("user", tuple(parts)))
        elif role == "assistant":
            raw = message.get("raw")
            if (isinstance(raw, dict) and raw.get("layer") == RAW_VERSION and isinstance(raw.get("items"), list)
                    and not message.get("interrupted")):
                out.extend(i for i in (item_from_json(r) for r in raw["items"] if isinstance(r, dict)) if i)
                continue
            text = assistant_text_of(message)
            if text:
                out.append(Message("assistant", (TextPart(text),)))
            for call in message.get("toolCalls") or []:
                out.append(ToolCall(str(call["id"]), call["name"], dict(call.get("args") or {})))
        elif role == "tool":
            for result in message.get("toolResults") or []:
                value = result.get("result")
                out.append(ToolResult(str(result["id"]), result.get("name", ""), value,
                                      isinstance(value, dict) and bool(value.get("error"))))
    return tuple(out)


# --- reporting back --------------------------------------------------------------------------

def reported_model(response: Response) -> str:
    return response.provenance.reported_model or split_endpoint_id(response.provenance.endpoint_id)[1]


def publish_completed(response: Response, *, session_id: str | None, background: bool) -> None:
    """Tell the cost ledger what a call really used — only what was reported."""
    usage = response.usage
    units = {k: v for k, v in (("unitsIn", usage.input), ("unitsOut", usage.output),
                               ("cachedIn", usage.cached)) if v is not None}
    if not units:
        return
    connection, _ = split_endpoint_id(response.provenance.endpoint_id)
    try:
        conn = config.current().connections.get(connection)
        provider = (conn.preset or conn.driver) if conn else connection
    except config.ConfigError:
        provider = connection
    model = reported_model(response)
    bus.publish(EventType.MODEL_CALL_COMPLETED, {"sessionId": session_id, "provider": provider, "model": model,
                                                 "modelId": model, "background": background, "usage": units})


_STAYED = ("Jarvis stays on the model you picked{model}. Choose Auto in the model list if you'd rather it work "
           "around problems like this.")


def _sentence(text: str) -> str:
    text = text.strip()
    return text if not text or text[-1] in ".!?…" else text + "."


def failure(err: ModelError, *, pinned_selection: bool) -> NoModelAvailable:
    """A layer error as the plain 'no model could answer' the app already knows how to say."""
    message = _sentence(str(err))
    if pinned_selection and err.type != "no_eligible_endpoint":
        model = f" ({split_endpoint_id(err.endpoint_id)[1]})" if err.endpoint_id and "/" in err.endpoint_id else ""
        message += " " + _STAYED.format(model=model)
    return NoModelAvailable(message, detail={"reason": err.type, "endpoint": err.endpoint_id})


def refuse_raw_arguments(response: Response) -> None:
    """A tool call whose arguments weren't valid JSON is never run."""
    for call in response.tool_calls:
        if call.arguments is None:
            raise NoModelAvailable(f"The model asked to use “{call.name}”, but its request was garbled, so nothing "
                                   "was run.", detail={"reason": "invalid_tool_arguments", "tool": call.name})
