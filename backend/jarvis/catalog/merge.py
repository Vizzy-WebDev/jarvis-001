"""Combining what we were told, what we found, and what we shipped knowing.

A version is almost never one source's answer. Its id is discovered fact, its
family is a pattern match, its context window is whatever the provider
volunteered — which for two of the three first-party APIs is nothing — and any
of it can be overridden by the person who owns the machine. So resolution is a
per-field merge under one precedence, and each field remembers where it came
from:

    user override  >  discovered from the provider  >  shipped catalog  >  unknown

The ordering is not a preference, it is a statement about reliability. A person
who has corrected a field knows something this code does not. A provider
reporting its own model's context window is authoritative in a way a pattern
match never is. And a pattern match, in turn, beats the previous build's
approach of inferring capability from whether the word "mini" appeared in the
name.

**Nothing here invents a value.** Where every source is silent the field stays
unknown and says so, because the alternative — filling the gap with a plausible
default — is how a model with no recorded vision support came to be routed an
image. Unknown is a state the rest of the system handles; a confident wrong
answer is not.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from .known import SEED, FamilyRule, match
from .spec import (
    UNKNOWN_EFFORT, Capabilities, Lifecycle, Source, Support, Version, effort_scheme_from,
)

#: An id ending in a date is a frozen snapshot; a bare one floats to whatever
#: the provider currently serves and can change underneath a running install.
#:
#: This is a read of the id's SHAPE, not an inference about the model — the
#: distinction that separates it from the name-guessing being removed. It is
#: still recorded as a catalog-level guess rather than fact, because a provider
#: is free to name things however it likes.
_DATED = re.compile(r"[-_@](20\d{2}[-_]?\d{2}[-_]?\d{2}|20\d{2}[-_]?\d{2})$")

_CAPABILITY_NAMES = ("tools", "vision", "video", "audio", "web_search")


def looks_pinned(model: str) -> Support:
    """Whether this id names one frozen snapshot, as far as its shape shows."""
    if not model:
        return Support.UNKNOWN
    return Support.YES if _DATED.search(model) else Support.NO


def _as_support(value: Any) -> Support | None:
    """Read a capability from a source that may speak booleans or nothing.

    `None` means the source did not answer, which is different from answering
    no — the caller keeps looking down the precedence chain instead of
    stopping here.
    """
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


def _as_lifecycle(value: Any) -> Lifecycle | None:
    """Read a lifecycle from a source that may speak strings.

    Strings matter because these facts round-trip through JSON on their way to
    and from a deployment's stored record: an enum written to disk comes back as
    its value, and a version of this function that only accepted the enum
    silently turned every restored "retired" into "unknown" — which is to say it
    forgot, on restart, exactly the thing retirement exists to remember.
    """
    if value is None:
        return None
    if isinstance(value, Lifecycle):
        return value
    if isinstance(value, str):
        try:
            return Lifecycle(value.lower())
        except ValueError:
            return None
    return None


def _merge_capabilities(
    user: Mapping[str, Any], discovered: Mapping[str, Any], rule: FamilyRule | None,
) -> tuple[Capabilities, dict[str, Source]]:
    """Per-FLAG precedence, not per-object.

    Merging whole capability objects would mean one discovered flag replacing
    every catalog flag beside it — a provider that reports only vision would
    silently erase a known-good tools answer.
    """
    user_caps = user.get("capabilities") or {}
    found_caps = discovered.get("capabilities") or {}
    rule_caps = rule.capabilities if rule else Capabilities()

    values: dict[str, Support] = {}
    provenance: dict[str, Source] = {}
    for name in _CAPABILITY_NAMES:
        for candidate, source in (
            (_as_support(user_caps.get(name)), Source.USER),
            (_as_support(found_caps.get(name)), Source.DISCOVERED),
            (rule_caps.get(name) if rule else None, Source.CATALOG),
        ):
            if candidate is not None and candidate is not Support.UNKNOWN:
                values[name] = candidate
                provenance[f"capabilities.{name}"] = source
                break
        else:
            values[name] = Support.UNKNOWN

    return Capabilities(**values), provenance


def _first(
    field: str,
    candidates: tuple[tuple[Any, Source], ...],
    provenance: dict[str, Source],
) -> Any:
    """The highest-precedence source that actually answered."""
    for value, source in candidates:
        if value is not None:
            provenance[field] = source
            return value
    return None


def resolve(
    *,
    model: str,
    provider: str | None = None,
    discovered: Mapping[str, Any] | None = None,
    user: Mapping[str, Any] | None = None,
    rules: tuple[FamilyRule, ...] = SEED,
) -> Version:
    """Everything known about one callable model id, with its sources attached.

    `provider` is the lineage's owner when the caller knows it. It is not the
    connection — a gateway reselling someone else's model is still serving that
    maker's model, and a catalog that recorded the plumbing instead would make
    the same version look like two.

    Safe to call knowing nothing but the id. That is the ordinary case for a
    local server or anything behind a gateway, and the result is a usable
    version with every fact marked unknown rather than a failure or a guess.
    """
    found: Mapping[str, Any] = discovered or {}
    given: Mapping[str, Any] = user or {}
    rule = match(model, rules)
    provenance: dict[str, Source] = {}

    resolved_provider = _first("provider", (
        (given.get("provider"), Source.USER),
        (provider, Source.DISCOVERED),
        (found.get("provider"), Source.DISCOVERED),
        (rule.provider if rule else None, Source.CATALOG),
    ), provenance) or "unknown"

    family = _first("family", (
        (given.get("family"), Source.USER),
        (rule.family if rule else None, Source.CATALOG),
    ), provenance)

    # The model id is the last resort for a label rather than a rule's family
    # name: "Claude Sonnet" is the family, and showing it as the label for four
    # different releases would make them indistinguishable in a picker.
    label = _first("label", (
        (given.get("label"), Source.USER),
        (found.get("label"), Source.DISCOVERED),
    ), provenance) or model

    pinned_value = _as_support(given.get("pinned"))
    if pinned_value is not None:
        provenance["pinned"] = Source.USER
        pinned = pinned_value
    else:
        provenance["pinned"] = Source.CATALOG
        pinned = looks_pinned(model)

    context_tokens = _first("context_tokens", (
        (given.get("context_tokens"), Source.USER),
        (found.get("context_tokens"), Source.DISCOVERED),
    ), provenance)

    capabilities, capability_provenance = _merge_capabilities(given, found, rule)
    provenance.update(capability_provenance)

    effort = effort_scheme_from(_first("effort", (
        (effort_scheme_from(given.get("effort")), Source.USER),
        (effort_scheme_from(found.get("effort")), Source.DISCOVERED),
        (rule.effort if rule else None, Source.CATALOG),
    ), provenance))
    if effort is None:
        effort = UNKNOWN_EFFORT
        provenance.pop("effort", None)

    quality = _first("quality", (
        (given.get("quality"), Source.USER),
        (rule.quality if rule else None, Source.CATALOG),
    ), provenance)

    lifecycle = _as_lifecycle(_first("lifecycle", (
        (given.get("lifecycle"), Source.USER),
        (found.get("lifecycle"), Source.DISCOVERED),
    ), provenance))
    if lifecycle is None:
        lifecycle = Lifecycle.UNKNOWN
        provenance.pop("lifecycle", None)

    return Version(
        provider=resolved_provider,
        model=model,
        label=label,
        family=family,
        pinned=pinned,
        context_tokens=context_tokens,
        capabilities=capabilities,
        effort=effort,
        quality=quality,
        lifecycle=lifecycle,
        provenance=provenance,
    )
