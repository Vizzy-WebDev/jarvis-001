"""Placeholder — filled in by a later stage of the model-layer rebuild."""

from __future__ import annotations

from typing import Any

from ..errors import Unavailable

NAME = "openai_chat"
QUIRKS = frozenset({"no_parallel_tool_calls", "tool_args_not_streamed", "rejects_strict", "no_stream_usage", "no_system_role", "no_tool_call_ids", "error_envelope_in_text", "reasoning_param", "max_tokens_param", "show_endpoint", "catalog_fields", })
DEFAULT_CAPABILITIES: dict[str, Any] = {"text_in": True, "streaming": True}


def stream(conn: Any, prepared: Any) -> Any:
    raise Unavailable("This driver isn't built yet.")


def discover(conn: Any) -> list[Any]:
    raise Unavailable("This driver isn't built yet.")


def translate_schema(schema: Any, quirks: Any = None) -> Any:
    return schema
