"""Placeholder — filled in by a later stage of the model-layer rebuild."""

from __future__ import annotations

from typing import Any

from ..errors import Unavailable

NAME = "anthropic_messages"
QUIRKS = frozenset({})
DEFAULT_CAPABILITIES: dict[str, Any] = {"text_in": True, "streaming": True}


def stream(conn: Any, prepared: Any) -> Any:
    raise Unavailable("This driver isn't built yet.")


def discover(conn: Any) -> list[Any]:
    raise Unavailable("This driver isn't built yet.")


def translate_schema(schema: Any, quirks: Any = None) -> Any:
    return schema
