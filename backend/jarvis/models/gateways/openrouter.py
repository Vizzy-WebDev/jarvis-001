"""OpenRouter's own listing fields (`/api/v1/models`), for a connection declared as
OpenRouter. Everything else about it is the plain chat format."""

from __future__ import annotations

from typing import Any

from ..request import REASONING_LEVELS
from . import _described


def facts(row: dict[str, Any]) -> dict[str, Any]:
    found = _described.describe(row)
    pricing = row.get("pricing") if isinstance(row.get("pricing"), dict) else {}
    # OpenRouter's own router products (Auto Router, Pareto Router, Fusion, Body Builder) mark
    # themselves this way — confirmed live, not documented. What they cost depends on which
    # underlying model answers, so the listing can't quote one; a plain rolling alias to one
    # current model (e.g. "~anthropic/claude-sonnet-latest") still prices normally and is
    # correctly left alone. `openrouter/free` prices at a real 0, not -1, so it isn't caught
    # here — disclosed, not missed: it behaves as an ordinary model when tried.
    if "prompt" in pricing:
        found["router"] = pricing.get("prompt") == "-1"
    supported = row.get("supported_parameters")
    if isinstance(supported, list) and supported:
        found["reasoning"] = ({"supported": True, "levels": list(REASONING_LEVELS), "default": None}
                              if "reasoning" in supported else {"supported": False})
    return found
