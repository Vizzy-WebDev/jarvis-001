"""What a model can be asked to do, and what content it accepts or produces.

**Every one of these is three-state, never two.** `Support.YES` / `NO` /
`UNKNOWN` are different answers: nobody having established whether a model
can see an image is not the same fact as it definitely being unable to, and
collapsing the two loses exactly the information a router needs. Only a
definite `NO` may ever exclude a candidate — see `model_system/router.py`. Answering
`UNKNOWN` as `NO` hides a capable model for no reason; answering it `YES`
sends a request a model will reject.

This applies equally to "can it call tools" and to "does it accept an image" —
an earlier draft of this file tracked modality support as a plain set,
reasoning that what a model accepts is usually just documented fact. That
reasoning does not hold for a freshly added local or unlisted model, which is
exactly the case a fresh install hits immediately: nobody has told this
system yet whether it can see an image, and a plain set has no way to say so.
So every capability below, modality support included, is `Support`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

#: The boundary in a camelCase name, so a requirement that arrived over the
#: wire as `webSearch` reads as `web_search`.
_SNAKE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

#: A short, older name for a capability -> its real field here. `need` dicts
#: are built all over the app, not only inside this package, and several
#: callers still say "video"/"audio"/"image" meaning "accepts video as
#: input" — the plain-English name for the common case, not a typo.
_ALIASES = {"video": "video_input", "audio": "audio_input", "image": "vision"}


class Support(Enum):
    """Whether a model can do something. `UNKNOWN` is a real, distinct answer —
    see this module's docstring."""

    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


#: Every capability this system tracks. Named once so a caller holding a
#: string (a routing requirement that arrived over the wire, a stored
#: override) has one place to validate and normalise it against. Text
#: input/output is deliberately absent — every model this system can call
#: does it, and a capability nothing can ever answer `NO` to is not a
#: capability, it's a constant.
CAPABILITY_NAMES: tuple[str, ...] = (
    "vision",              # accepts an image
    "audio_input",         # accepts audio
    "video_input",         # accepts video
    "image_generation",    # produces an image
    "audio_generation",    # produces audio (speech)
    "embeddings",          # produces an embedding vector instead of text
    "tool_calling",
    "parallel_tool_calls",
    "structured_output",   # JSON/schema-constrained output
    "reasoning",           # has any reasoning/thinking control at all
    "streaming",
    "web_search",
    "computer_use",
)


@dataclass(frozen=True)
class Capabilities:
    """What a model can be asked to do. Everything defaults to unknown until
    something — the catalog seed, a provider's own listing, or a person —
    says otherwise. Never inferred from a provider's name or another model in
    its family."""

    vision: Support = Support.UNKNOWN
    audio_input: Support = Support.UNKNOWN
    video_input: Support = Support.UNKNOWN
    image_generation: Support = Support.UNKNOWN
    audio_generation: Support = Support.UNKNOWN
    embeddings: Support = Support.UNKNOWN
    tool_calling: Support = Support.UNKNOWN
    parallel_tool_calls: Support = Support.UNKNOWN
    structured_output: Support = Support.UNKNOWN
    reasoning: Support = Support.UNKNOWN
    streaming: Support = Support.UNKNOWN
    web_search: Support = Support.UNKNOWN
    computer_use: Support = Support.UNKNOWN

    def get(self, name: str | None) -> Support:
        """Read a capability by name, for a caller holding a plain string
        (a routing requirement, a stored override). `webSearch` and
        `web_search` name the same capability, and so do `video` and
        `video_input` — normalised here, the one place a capability is read
        by string, so a real requirement never silently excludes nothing for
        having been spelled the other way. An unrecognised or malformed name
        still answers `UNKNOWN` rather than raising — a requirement this
        build has never heard of should exclude nothing and crash nothing."""
        value = getattr(self, Capabilities.normalise(name), None)
        return value if isinstance(value, Support) else Support.UNKNOWN

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name).value for name in CAPABILITY_NAMES}

    @staticmethod
    def normalise(name: str | None) -> str:
        """`webSearch`/`web_search`, `video`/`video_input` — the one place a
        capability name from OUTSIDE this module (a routing requirement, a
        stored override) is translated to this module's own field name.
        `get()` uses this internally; a caller that needs to compare a name
        against a FIXED SET of canonical names (`ai/router.py`'s
        `MUST_BE_CERTAIN`) must normalise first, or a differently-spelled
        requirement silently fails to match anything in that set."""
        if not isinstance(name, str) or not name:
            return ""
        normalised = _SNAKE.sub("_", name).lower()
        return _ALIASES.get(normalised, normalised)


def _support_of(value: Any) -> Support | None:
    """Read one capability's value from a source that may speak booleans,
    strings, or nothing. `None` means the source did not answer."""
    if value is None:
        return None
    if isinstance(value, Support):
        return value
    if isinstance(value, bool):
        return Support.YES if value else Support.NO
    if isinstance(value, str):
        try:
            return Support(value.lower())
        except ValueError:
            return None
    return None


def capabilities_from_dict(value: Mapping[str, Any] | None) -> Capabilities:
    """A `Capabilities` from stored JSON — never raises. A hand-edited or
    corrupt record degrades to "we do not know" per field, never to a crash
    that would take the whole registry down with it."""
    data = value or {}
    fields: dict[str, Any] = {}
    for name in CAPABILITY_NAMES:
        support = _support_of(data.get(name))
        if support is not None:
            fields[name] = support
    return Capabilities(**fields)


def merge_capabilities(*sources: Mapping[str, Any] | Capabilities | None) -> Capabilities:
    """Per-FIELD precedence across any number of sources, first wins.

    Merging whole `Capabilities` objects would let one source's silence on
    every field overwrite another's real answers; per-field merge is what
    keeps a provider that reports only `vision` from erasing a catalog-known
    `tool_calling: YES` sitting beside it.

    Callers pass sources highest-precedence first (user overrides, then
    discovered, then catalog seed).
    """
    parsed = [
        s if isinstance(s, Capabilities) else capabilities_from_dict(s)
        for s in sources if s is not None
    ]
    fields: dict[str, Any] = {}
    for name in CAPABILITY_NAMES:
        for cap in parsed:
            support = cap.get(name)
            if support is not Support.UNKNOWN:
                fields[name] = support
                break
    return Capabilities(**fields)
