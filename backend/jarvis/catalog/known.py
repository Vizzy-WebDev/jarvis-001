"""The small amount Jarvis ships knowing, and the reasons it is small.

Two things make this file different from the table it replaces.

**It is keyed by PATTERN, not by exact model id.** The old `KNOWN` dict looked
up `KNOWN.get(model)` — an exact-match dictionary read, keyed on bare names
like `"claude-haiku-4-5"`. Provider listings return dated snapshots
(`claude-haiku-4-5-20251001`), so one character of difference dropped a model
straight through to name-regex guessing. That is not a table anyone can keep up
to date; it is a table that is wrong by construction, because the ids it is
keyed on are not the ids that arrive. A pattern matches a lineage, so a version
released tomorrow lands in its family on the day it ships, with no edit here.

**It carries only what the pipeline needs and cannot get another way.** Every
field was tested against two questions: does something actually require it, and
could discovery or measurement supply it? Price and speed failed both — Jarvis
already records real spend, and latency is measurable from an event that
already fires, and a measured number beats an authored one every time. They are
gone rather than ported. What is left is the reasoning scheme, which no
first-party API exposes, and the family a version belongs to, which no API
exposes either.

**`SEED` may be empty, and the architecture has to survive that.** A catalog
that only works when it has an entry for your model is a hardcoded model list
wearing a hat. `tests/test_catalog.py` runs the whole resolution path against an
empty seed and asserts a usable version comes out the other side — everything
unknown, nothing invented, still callable. That test is the real specification
of this file; the entries below are a convenience on top of it.

A rule's `provider` is the lineage's owner, which is NOT necessarily the
connection the model is reached through. A gateway reselling someone else's
model is still serving that maker's model, and saying otherwise would make the
catalog describe the plumbing instead of the thing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Pattern

from .spec import Capabilities, Effort, EffortKind, EffortScheme, Support

#: Token budgets for the levels of a BUDGET-shaped scheme.
#:
#: A mapping decision rather than a fact about any provider: the provider takes
#: a number, and what Jarvis's "medium" is worth in tokens is ours to choose.
#: Named here so the choice is in one place and legible, rather than scattered
#: as literals through the rules below.
#:
#: OFF is zero, and the adapter reads zero as "send no thinking block at all"
#: rather than "a budget of nothing" — the two are the same intent, and only
#: one of them is a request a provider will accept.
BUDGET_TOKENS = {
    Effort.OFF: 0,
    Effort.MINIMAL: 1024,
    Effort.LOW: 4096,
    Effort.MEDIUM: 16384,
    Effort.HIGH: 32768,
    Effort.MAX: 65536,
}

#: The OpenAI-shaped `reasoning_effort` values, taken from the installed SDK's
#: own literal type rather than from memory. It accepts an `xhigh` between high
#: and max which Jarvis does not expose as a rung — see `Effort` for why.
_OPENAI_TIERS = {
    Effort.OFF: "none",
    Effort.MINIMAL: "minimal",
    Effort.LOW: "low",
    Effort.MEDIUM: "medium",
    Effort.HIGH: "high",
    Effort.MAX: "max",
}

#: Gemini's `ThinkingLevel` enum, which has no OFF and stops at HIGH. A version
#: using it therefore offers a SHORTER ladder than an OpenAI-shaped one, which
#: is the case the clamp exists for.
_GEMINI_LEVELS = {
    Effort.MINIMAL: "MINIMAL",
    Effort.LOW: "LOW",
    Effort.MEDIUM: "MEDIUM",
    Effort.HIGH: "HIGH",
}


def _scheme(kind: EffortKind, native: dict[Effort, object], default: Effort) -> EffortScheme:
    """Build a scheme from its native mapping, so the levels it claims and the
    levels it can actually send are the same set by construction."""
    return EffortScheme(kind=kind, levels=tuple(sorted(native)), default=default,
                        native=dict(native))


def _openai_tiers(default: Effort = Effort.MEDIUM) -> EffortScheme:
    return _scheme(EffortKind.TIERS, _OPENAI_TIERS, default)


def _gemini_levels(default: Effort = Effort.MEDIUM) -> EffortScheme:
    return _scheme(EffortKind.TIERS, _GEMINI_LEVELS, default)


def _budget(default: Effort = Effort.MEDIUM) -> EffortScheme:
    return _scheme(EffortKind.BUDGET, BUDGET_TOKENS, default)


@dataclass(frozen=True)
class FamilyRule:
    """One lineage, and what is true of everything in it.

    `capabilities` is left almost entirely unknown on purpose. A rule should
    assert what it genuinely knows about a LINE of models; per-version facts
    like vision support change between releases of the same family, and
    discovery reports them for real on the providers that bother to.
    """

    provider: str
    family: str
    label: str
    pattern: Pattern[str]
    effort: EffortScheme
    capabilities: Capabilities = field(default_factory=Capabilities)
    quality: int | None = None


#: Matched in order; the first hit wins, so narrower patterns come first.
#:
#: Deliberately short. Every entry here is a claim that will age, and the cost
#: of a wrong one is a model described incorrectly until someone notices —
#: whereas the cost of a MISSING one is a model described as unknown, which the
#: rest of the system already handles correctly. Absent beats wrong, so when in
#: doubt an entry does not get added.
SEED: tuple[FamilyRule, ...] = (
    FamilyRule(
        provider="anthropic", family="claude-opus", label="Claude Opus",
        pattern=re.compile(r"(^|/)claude-opus[-.]", re.I),
        effort=_budget(Effort.MEDIUM),
        capabilities=Capabilities(tools=Support.YES),
        quality=5,
    ),
    FamilyRule(
        provider="anthropic", family="claude-sonnet", label="Claude Sonnet",
        pattern=re.compile(r"(^|/)claude-sonnet[-.]", re.I),
        effort=_budget(Effort.MEDIUM),
        capabilities=Capabilities(tools=Support.YES),
        quality=4,
    ),
    FamilyRule(
        provider="anthropic", family="claude-haiku", label="Claude Haiku",
        pattern=re.compile(r"(^|/)claude-haiku[-.]", re.I),
        effort=_budget(Effort.LOW),
        capabilities=Capabilities(tools=Support.YES),
        quality=3,
    ),
    FamilyRule(
        provider="google", family="gemini-pro", label="Gemini Pro",
        pattern=re.compile(r"(^|/)gemini[-.\d]*-pro", re.I),
        effort=_gemini_levels(Effort.MEDIUM),
        capabilities=Capabilities(tools=Support.YES),
        quality=5,
    ),
    FamilyRule(
        provider="google", family="gemini-flash", label="Gemini Flash",
        pattern=re.compile(r"(^|/)gemini[-.\d]*-flash", re.I),
        effort=_gemini_levels(Effort.LOW),
        capabilities=Capabilities(tools=Support.YES),
        quality=3,
    ),
    FamilyRule(
        provider="openai", family="gpt", label="GPT",
        pattern=re.compile(r"(^|/)gpt-", re.I),
        effort=_openai_tiers(Effort.MEDIUM),
        capabilities=Capabilities(tools=Support.YES),
        quality=4,
    ),
)


def match(model: str, rules: tuple[FamilyRule, ...] = SEED) -> FamilyRule | None:
    """The first rule whose pattern recognises this model id, or None.

    None is an ordinary outcome, not a failure: most models a user adds — every
    local one, everything behind a gateway with its own naming — will not match
    anything here, and the resolution path is built for that.
    """
    for rule in rules:
        if rule.pattern.search(model or ""):
            return rule
    return None
