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

VOCABULARY_VERSION = 2

FLAGS = frozenset({
    "text_in", "image_in", "pdf_in",
    "tools", "parallel_tools",
    "structured_output_strict", "json_mode",
    "reasoning_control", "prompt_caching", "streaming", "embeddings",
})
LIMITS = frozenset({"max_context_tokens", "max_output_tokens"})
#: v2: the canonical effort levels (`types.EFFORTS`) an endpoint with `reasoning_control`
#: accepts, in order, and the one it uses when none is sent.
EFFORT_LEVELS, EFFORT_DEFAULT = "effort_levels", "effort_default"
VOCABULARY = FLAGS | LIMITS | {EFFORT_LEVELS, EFFORT_DEFAULT}

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
    from .types import EFFORTS

    if name in FLAGS and not isinstance(value, bool):
        raise ValueError(f"capability {name} must be true or false")
    if name in LIMITS and (not isinstance(value, int) or isinstance(value, bool) or value <= 0):
        raise ValueError(f"capability {name} must be a positive whole number")
    if name == EFFORT_LEVELS and (not isinstance(value, list) or not value or len(set(value)) != len(value)
                                  or any(v not in EFFORTS for v in value)):
        raise ValueError(f"capability {name} must be a list of different levels from: {', '.join(EFFORTS)}")
    if name == EFFORT_DEFAULT and value not in EFFORTS:
        raise ValueError(f"capability {name} must be one of: {', '.join(EFFORTS)}")


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


def effort_levels(caps: Mapping[str, CapValue]) -> tuple[str, ...]:
    """The canonical levels this endpoint accepts, in canonical order: none at all
    unless it takes a reasoning control and its levels are known. Never assumed."""
    from .types import EFFORTS

    found = caps.get(EFFORT_LEVELS)
    if not has(caps, "reasoning_control") or found is None or not isinstance(found.value, (list, tuple)):
        return ()
    return tuple(level for level in EFFORTS if level in found.value)


def effort_default(caps: Mapping[str, CapValue]) -> str | None:
    found = caps.get(EFFORT_DEFAULT)
    return found.value if found is not None and found.value in effort_levels(caps) else None


def nearest_effort(asked: str, accepted: tuple[str, ...]) -> str | None:
    """The accepted level nearest the one asked for; equally near two, the higher."""
    from .types import EFFORTS

    if not accepted:
        return None
    rank = EFFORTS.index(asked)
    return min(accepted, key=lambda level: (abs(EFFORTS.index(level) - rank), -EFFORTS.index(level)))
