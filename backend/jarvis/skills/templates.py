"""`{{inputs.x}}` and `{{steps.a.result}}` inside a pipeline step.

Pure and dependency-free. Two jobs: find every reference (for validation, before
anything runs) and resolve one (at run time, against what has actually happened).

**An unresolved reference raises rather than becoming an empty string.** A step
silently running with a blank argument is the failure mode this exists to
prevent — it produces a plausible-looking result built on nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

REF = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}")
_WHOLE = re.compile(r"^\s*\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}\s*$")


@dataclass(frozen=True)
class Reference:
    ref: str
    root: str | None
    id: str | None = None
    path: tuple[str, ...] = ()


def parse_ref(ref: str) -> Reference:
    parts = str(ref).split(".")
    if parts[0] == "inputs":
        if len(parts) != 2:
            return Reference(ref=ref, root=None)
        return Reference(ref=ref, root="inputs", id=parts[1])
    if parts[0] == "steps":
        if len(parts) < 2:
            return Reference(ref=ref, root=None)
        return Reference(ref=ref, root="steps", id=parts[1], path=tuple(parts[2:]))
    return Reference(ref=ref, root=None)


def extract_refs(value: Any) -> list[Reference]:
    """Every reference anywhere inside a value — for validation, not resolution.
    Duplicates are kept: each occurrence is its own possible mistake."""
    found: list[Reference] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            found.extend(parse_ref(m.group(1)) for m in REF.finditer(node))
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for item in node.values():
                walk(item)

    walk(value)
    return found


class Unresolved(ValueError):
    """A reference that could not be looked up. Names the exact reference."""


def _lookup(ref: str, context: dict[str, Any]) -> tuple[bool, Any]:
    parsed = parse_ref(ref)
    if parsed.root == "inputs":
        inputs = context.get("inputs") or {}
        if parsed.id not in inputs:
            return False, None
        return True, inputs[parsed.id]
    if parsed.root == "steps":
        step = (context.get("steps") or {}).get(parsed.id)
        # A step that failed, or has not run, counts as not found: a step can
        # only ever see a PRIOR, SUCCESSFUL step's data.
        if not step or step.get("ok") is not True:
            return False, None
        value = step.get("result")
        for segment in parsed.path:
            if not isinstance(value, dict) or segment not in value:
                return False, None
            value = value[segment]
        return True, value
    return False, None


def resolve(value: Any, context: dict[str, Any]) -> Any:
    """Resolve every reference in a value.

    A string that is ENTIRELY one reference keeps the real value's type — a
    list stays a list, a number stays a number. A reference inside a larger
    string is stringified in place.
    """
    if isinstance(value, str):
        whole = _WHOLE.match(value)
        if whole:
            found, resolved = _lookup(whole.group(1), context)
            if not found:
                raise Unresolved(f'"{{{{{whole.group(1)}}}}}" could not be resolved — check '
                                 f"the input or step exists and has already run.")
            return resolved

        def replace(match: re.Match[str]) -> str:
            found, resolved = _lookup(match.group(1), context)
            if not found:
                raise Unresolved(f'"{{{{{match.group(1)}}}}}" could not be resolved — check '
                                 f"the input or step exists and has already run.")
            return resolved if isinstance(resolved, str) else str(resolved)

        return REF.sub(replace, value)
    if isinstance(value, list):
        return [resolve(item, context) for item in value]
    if isinstance(value, dict):
        return {key: resolve(item, context) for key, item in value.items()}
    return value
