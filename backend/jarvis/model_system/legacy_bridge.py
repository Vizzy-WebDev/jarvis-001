"""Translating the orchestrator's plain-dict transcript shape into this
system's own normalized `Message`/`ToolDefinition` types.

The transcript store (`jarvis/conversation.py`) and the capability registry
(`jarvis/capabilities/registry.py`) both predate this rebuild and speak in
plain dicts — `{"role", "text", "media", "toolCalls", "toolResults", "raw",
"interrupted"}` for a message, `{"name", "description", "parameters"}` for a
tool. Neither is part of the AI Model System being rebuilt here, so neither
is rewritten; this module is the one seam between "how Jarvis stores a
conversation" and "how the model system represents one."
"""

from __future__ import annotations

import base64
import json
from typing import Any

from .request import Attachment, Message, Modality, ToolCall, ToolDefinition

_MEDIA_KIND_TO_MODALITY = {
    "image": Modality.IMAGE, "video": Modality.VIDEO,
    "audio": Modality.AUDIO, "file": Modality.FILE,
}


def _attachment_from_media(item: dict[str, Any]) -> Attachment:
    modality = _MEDIA_KIND_TO_MODALITY.get(item.get("kind") or "image", Modality.IMAGE)
    data = base64.b64decode(item["dataBase64"]) if item.get("dataBase64") else None
    return Attachment(modality=modality, mime_type=item.get("mimeType") or "image/png",
                      data=data, uri=item.get("uri"))


def message_from_dict(entry: dict[str, Any]) -> tuple[Message, ...]:
    """One legacy-shaped message -> zero or more `Message`s. A "tool" entry
    carrying several results expands into one `Message` per result, since
    that is this system's own per-result shape."""
    role = entry.get("role")
    if role == "user":
        media = entry.get("media") or []
        attachments = tuple(_attachment_from_media(m) for m in media if isinstance(m, dict))
        return (Message(role="user", text=entry.get("text") or "", attachments=attachments),)

    if role == "assistant":
        # An interrupted turn's raw content holds everything generated AFTER
        # the user cut in — replaying it would put words back in that were
        # never actually heard. See the orchestrator's own interrupt handling.
        raw = None if entry.get("interrupted") else entry.get("raw")
        tool_calls_raw = entry.get("toolCalls")
        if tool_calls_raw:
            calls = tuple(ToolCall(c.get("id", ""), c.get("name", ""), c.get("args") or {})
                          for c in tool_calls_raw)
            return (Message(role="assistant", text=entry.get("text") or "", tool_calls=calls, raw=raw),)
        return (Message(role="assistant", text=entry.get("text") or "", raw=raw),)

    if role == "tool":
        results = entry.get("toolResults") or []
        return tuple(
            Message(role="tool", tool_call_id=r.get("id"), tool_name=r.get("name"),
                   text=json.dumps(r.get("result"), default=str))
            for r in results if isinstance(r, dict)
        )
    return ()


def messages_from_dicts(entries: list[dict[str, Any]] | None) -> tuple[Message, ...]:
    out: list[Message] = []
    for entry in entries or ():
        out.extend(message_from_dict(entry))
    return tuple(out)


def tool_from_declaration(decl: dict[str, Any]) -> ToolDefinition:
    return ToolDefinition(name=decl.get("name", ""), description=decl.get("description", ""),
                          parameters=decl.get("parameters") or {"type": "object", "properties": {}})


def tools_from_declarations(decls: list[dict[str, Any]] | None) -> tuple[ToolDefinition, ...]:
    return tuple(tool_from_declaration(d) for d in decls or ())
