"""Finish: from what a driver said to a Response.

Output items are assembled in the order they arrived (text, provider state, tool
calls), provider state is sealed with the endpoint that made it, usage gets an
estimated cost, and a JSON output is parsed and validated. The text itself is
never changed: `data` is the parsed value, the items keep exactly what was said.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .catalog import Endpoint
from .types import (Finish, Item, Message, Sealed, SealedEvent, TextDelta, TextPart, ToolCallCompleted, Usage)

_FENCE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL | re.IGNORECASE)


@dataclass
class Assembled:
    items: list[Item] = field(default_factory=list)
    finish: Finish | None = None
    _text: list[str] = field(default_factory=list)

    def _flush_text(self) -> None:
        if self._text:
            self.items.append(Message("assistant", (TextPart("".join(self._text)),)))
            self._text = []

    def add(self, event: Any, endpoint_id: str) -> None:
        if isinstance(event, TextDelta):
            self._text.append(event.text)
        elif isinstance(event, SealedEvent):
            self._flush_text()
            self.items.append(Sealed(endpoint_id, event.kind, event.payload))
        elif isinstance(event, ToolCallCompleted):
            self._flush_text()
            self.items.append(event.call)
        elif isinstance(event, Finish):
            self._flush_text()
            self.finish = event

    @property
    def text(self) -> str:
        return "".join(i.text for i in self.items if isinstance(i, Message)) + "".join(self._text)


def parse_json(text: str) -> tuple[bool, Any]:
    """(ok, value). Reads the reply as JSON; a code fence around it is looked past
    for PARSING only — the reply itself is not altered."""
    candidates = [text]
    fenced = _FENCE.match(text or "")
    if fenced:
        candidates.append(fenced.group(1))
    for candidate in candidates:
        try:
            return True, json.loads(candidate)
        except (TypeError, ValueError):
            continue
    return False, None


def validation_errors(value: Any, schema: Any) -> list[str]:
    try:
        validator = Draft202012Validator(schema)
    except SchemaError as err:
        return [f"the schema itself is invalid: {err.message}"]
    errors = sorted(validator.iter_errors(value), key=lambda e: list(e.path))
    out = []
    for err in errors[:8]:
        where = "/".join(str(p) for p in err.path) or "(top level)"
        out.append(f"{where}: {err.message}")
    return out


def check_output(text: str, schema: Any) -> tuple[Any, list[str]]:
    ok, value = parse_json(text)
    if not ok:
        return None, ["the reply isn't valid JSON"]
    return value, validation_errors(value, schema)


def cost_of(endpoint: Endpoint, usage: Usage) -> Usage:
    if endpoint.pricing is None or usage.cost is not None:
        return usage
    cost = endpoint.pricing.cost(input_tokens=usage.input, output_tokens=usage.output, cached_tokens=usage.cached)
    return Usage(input=usage.input, output=usage.output, cached=usage.cached, reasoning=usage.reasoning,
                 cost=cost, raw=usage.raw)


def add_usage(a: Usage | None, b: Usage) -> Usage:
    """Two calls' usage, as one (a repair attempt counts toward the call it repaired)."""
    if a is None:
        return b

    def plus(x: int | float | None, y: int | float | None) -> Any:
        return None if x is None and y is None else (x or 0) + (y or 0)

    return Usage(input=plus(a.input, b.input), output=plus(a.output, b.output), cached=plus(a.cached, b.cached),
                 reasoning=plus(a.reasoning, b.reasoning), cost=plus(a.cost, b.cost),
                 raw={"calls": [dict(a.raw), dict(b.raw)]})
