"""The capabilities the layer knows how to read, and how the sources of a capability's value
are merged.

The vocabulary is open. A capability is a name and a value; a name this version has never seen
is carried, merged, required and reported exactly like a known one, and counts as present only
on an endpoint that declares, lists or proves it. Known names only add a type check and a plain
phrase for rejections.

A value comes from a driver's wire default, a model profile or the person's config
(`declared`), from what a server listed about its models (`discovered`), or from actually
trying it (`probed`). Probed beats discovered beats declared, and a disagreement is logged: it usually
means a config entry or a server's own listing is wrong.

A name the layer never interprets (anything outside the known set, including the
`x-<provider>:<name>` spelling) may hold any plain value; only a driver or the caller who asked
for it gives it meaning.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal, Mapping

logger = logging.getLogger(__name__)

VOCABULARY_VERSION = 3
import re

_NAME = re.compile(r"^[a-z][a-z0-9_.:\-]*$")

FLAGS = frozenset({
    "text_in", "image_in", "pdf_in",
    "tools", "parallel_tools",
    "structured_output_strict", "json_mode",
    "prompt_caching", "streaming",
    "reasoning_can_disable",
})
LIMITS = frozenset({"max_context_tokens", "max_output_tokens"})
#: v3: what a model is for. Generation routing only ever considers `chat`; embeddings only
#: `embedding`. Any other word is a kind too (the ones below are only the ones met so far).
KIND = "kind"
KINDS = ("chat", "embedding", "speech", "image", "other")
#: v3: the thinking levels this endpoint takes (its own names, in its own order), the one it
#: uses when none is sent, and whether thinking can be switched off. Unknown levels mean no control.
REASONING_LEVELS, REASONING_DEFAULT = "reasoning_levels", "reasoning_default"
VOCABULARY = FLAGS | LIMITS | {KIND, REASONING_LEVELS, REASONING_DEFAULT}

#: The canonical ordered scale a caller may ask on. An endpoint may list levels outside it;
#: those are kept as named, selectable by name, and never chosen by mapping.
EFFORT_SCALE = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

#: Weakest to strongest: a driver's wire default, a model profile, what the server listed,
#: what was actually tried, and what the person wrote in their own config.
Source = Literal["declared", "profile", "discovered", "probed", "configured"]


@dataclass(frozen=True)
class CapValue:
    value: Any
    source: Source


class UnknownCapability(ValueError):
    pass


def is_extension(name: str) -> bool:
    return name.startswith("x-") and ":" in name and len(name.split(":", 1)[1]) > 0


def check_name(name: str) -> None:
    """Any plain lower-case name will do; only something that can't be a name is refused."""
    if not isinstance(name, str) or not _NAME.match(name):
        raise UnknownCapability(
            f"“{name}” can't be a capability name: use lower-case letters, digits and _ (for example "
            "video_in).")


def check_value(name: str, value: Any) -> None:
    if name in FLAGS and not isinstance(value, bool):
        raise ValueError(f"capability {name} must be true or false")
    if name in LIMITS and (not isinstance(value, int) or isinstance(value, bool) or value <= 0):
        raise ValueError(f"capability {name} must be a positive whole number")
    if name == KIND and (not isinstance(value, str) or not _NAME.match(value)):
        raise ValueError(f"capability {name} must be a one-word kind, like chat or embedding")
    if name == REASONING_LEVELS and (not isinstance(value, list) or not value or len(set(value)) != len(value)
                                     or any(not isinstance(v, str) or not v.strip() for v in value)):
        raise ValueError(f"capability {name} must be a list of different, non-empty level names")
    if name == REASONING_DEFAULT and (not isinstance(value, str) or not value.strip()):
        raise ValueError(f"capability {name} must be a level name")


def merge(*, declared: Mapping[str, Any] | None = None, profile: Mapping[str, Any] | None = None,
          discovered: Mapping[str, Any] | None = None, probed: Mapping[str, Any] | None = None,
          configured: Mapping[str, Any] | None = None, where: str = "") -> dict[str, CapValue]:
    """One value per capability, from the strongest source that has one."""
    out: dict[str, CapValue] = {}
    for source, values in (("declared", declared), ("profile", profile), ("discovered", discovered),
                           ("probed", probed), ("configured", configured)):
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


def reasoning_levels(caps: Mapping[str, CapValue]) -> tuple[str, ...]:
    """This endpoint's own thinking levels, in its order. Empty = no control; never assumed."""
    found = caps.get(REASONING_LEVELS)
    if found is None or not isinstance(found.value, (list, tuple)):
        return ()
    return tuple(str(v) for v in found.value)


#: Where a model's own default may come from. A default is a fact about one model that only the
#: server that lists it, a measurement or the person can state; a wire default or a profile
#: shared by a family could only guess it, and a wrong one would be shown as the model's own.
DEFAULT_EVIDENCE = frozenset({"discovered", "probed", "configured"})


def reasoning_default(caps: Mapping[str, CapValue]) -> str | None:
    found = caps.get(REASONING_DEFAULT)
    if found is None or found.source not in DEFAULT_EVIDENCE:
        return None
    return found.value if found.value in reasoning_levels(caps) else None


def can_disable(caps: Mapping[str, CapValue]) -> bool:
    return has(caps, "reasoning_can_disable")


def kind_of(caps: Mapping[str, CapValue]) -> str | None:
    found = caps.get(KIND)
    return found.value if found is not None and isinstance(found.value, str) and found.value else None


def is_kind(caps: Mapping[str, CapValue], kind: str) -> bool:
    return kind_of(caps) == kind


def accepted_levels(caps: Mapping[str, CapValue]) -> tuple[str, ...]:
    """What may be asked: the endpoint's levels, plus `none` when thinking can be switched off."""
    levels = reasoning_levels(caps)
    if not levels:
        return ()
    return levels if "none" in levels or not can_disable(caps) else ("none", *levels)


def map_effort(asked: str, caps: Mapping[str, CapValue]) -> tuple[str | None, str]:
    """(level to send, how it was reached). How is "exact", "nearest", "none" (nothing sent:
    no control, or a provider level this endpoint doesn't list) — the caller reports it.
    Mapping only ever lands on a level of the canonical scale; the nearest wins, a tie goes
    to the higher."""
    accepted = accepted_levels(caps)
    if not accepted:
        return None, "none"
    if asked in accepted:
        return asked, "exact"
    if asked not in EFFORT_SCALE:
        return None, "none"
    rank = EFFORT_SCALE.index(asked)
    candidates = [level for level in accepted if level in EFFORT_SCALE]
    if not candidates:
        return None, "none"
    best = min(candidates, key=lambda level: (abs(EFFORT_SCALE.index(level) - rank), -EFFORT_SCALE.index(level)))
    return best, "nearest"
