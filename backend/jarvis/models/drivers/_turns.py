"""Reading canonical items as conversation turns — the grouping every wire format
needs before it can spell them its own way.

A turn is a run of items from one side: a user message; an assistant's words, tool
calls and provider state together; or the tool results that answer them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from ..prepared import Prepared
from ..types import Item, Message, Sealed, ToolCall, ToolResult


@dataclass
class Turn:
    role: Literal["user", "assistant", "tool"]
    items: list[Item] = field(default_factory=list)


def _side(item: Item) -> str:
    if isinstance(item, Message):
        return item.role
    if isinstance(item, ToolResult):
        return "tool"
    return "assistant"  # ToolCall, Sealed


def turns(items: tuple[Item, ...] | list[Item]) -> list[Turn]:
    out: list[Turn] = []
    for item in items:
        side = _side(item)
        if out and out[-1].role == side and side != "user":
            out[-1].items.append(item)
        else:
            out.append(Turn(side, [item]))  # type: ignore[arg-type]
    return out


def native_ids(items: tuple[Item, ...] | list[Item]) -> dict[str, Any]:
    """canonical id -> this endpoint's own id, from its Sealed "ids" items. The items
    handed to a driver hold only its own endpoint's provider state."""
    found: dict[str, Any] = {}
    for item in items:
        if isinstance(item, Sealed) and item.kind == "ids" and isinstance(item.payload, dict):
            found.update(item.payload)
    return found


def system_text(prepared: Prepared) -> str:
    return "\n\n".join(s.text for s in prepared.system)


def tool_calls(turn: Turn) -> list[ToolCall]:
    return [i for i in turn.items if isinstance(i, ToolCall)]


def text_of(turn: Turn) -> str:
    return "".join(i.text for i in turn.items if isinstance(i, Message))
