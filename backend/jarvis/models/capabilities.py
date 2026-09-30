"""The capability vocabulary — fixed and versioned — and how the three sources of a
capability's value are merged.

A value comes from config or a driver's defaults (`declared`), from what a server
listed about its models (`discovered`), or from actually trying it (`probed`).
Probed beats discovered beats declared, and a disagreement is logged: it usually
means a config entry or a server's own listing is wrong.

A provider-specific capability is written `x-<provider>:<name>` and may hold any
value; the layer never interprets one, only a driver does.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal, Mapping

logger = logging.getLogger(__name__)

VOCABULARY_VERSION = 1

FLAGS = frozenset({
    "text_in", "image_in", "pdf_in",
    "tools", "parallel_tools",
    "structured_output_strict", "json_mode",
    "reasoning_control", "prompt_caching", "streaming", "embeddings",
})
LIMITS = frozenset({"max_context_tokens", "max_output_tokens"})
VOCABULARY = FLAGS | LIMITS

Source = Literal["declared", "discovered", "probed"]
_RANK = {"declared": 0, "discovered": 1, "probed": 2}


@dataclass(frozen=True)
class CapValue:
    value: Any
    source: Source


class UnknownCapability(ValueError):
    pass


def is_extension(name: str) -> bool:
    return name.startswith("x-") and ":" in name and len(name.split(":", 1)[1]) > 0


def check_name(name: str) -> None:
    if name not in VOCABULARY and not is_extension(name):
        raise UnknownCapability(
            f"“{name}” isn't a capability this version knows (vocabulary v{VOCABULARY_VERSION}). "
            f"Use one of: {', '.join(sorted(VOCABULARY))}, or x-<provider>:<name>.")


def check_value(name: str, value: Any) -> None:
    if name in FLAGS and not isinstance(value, bool):
        raise ValueError(f"capability {name} must be true or false")
    if name in LIMITS and (not isinstance(value, int) or isinstance(value, bool) or value <= 0):
        raise ValueError(f"capability {name} must be a positive whole number")


def merge(*, declared: Mapping[str, Any] | None = None, discovered: Mapping[str, Any] | None = None,
          probed: Mapping[str, Any] | None = None, where: str = "") -> dict[str, CapValue]:
    """One value per capability, from the strongest source that has one."""
    out: dict[str, CapValue] = {}
    for source, values in (("declared", declared), ("discovered", discovered), ("probed", probed)):
        for name, value in (values or {}).items():
            if value is None:
                continue
            previous = out.get(name)
            if previous is not None and previous.value != value:
                logger.info("capability %s on %s: %s says %r, %s says %r — using %s",
                            name, where or "an endpoint", previous.source, previous.value,
                            source, value, source)
            out[name] = CapValue(value, source)  # type: ignore[arg-type]
    return out


def has(caps: Mapping[str, CapValue], name: str) -> bool:
    found = caps.get(name)
    return bool(found and found.value)


def limit(caps: Mapping[str, CapValue], name: str) -> int | None:
    found = caps.get(name)
    return found.value if found and isinstance(found.value, int) else None


def plain(caps: Mapping[str, CapValue]) -> dict[str, Any]:
    return {k: v.value for k, v in caps.items()}
