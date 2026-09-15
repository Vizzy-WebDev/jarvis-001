"""One candidate builder — for chat, one-off calls, and the control loop alike.

In the Node implementation there are three ways to pick a model and they do not
agree: the router ranks for conversation, `ai.js` re-implements a candidate walk
with its own capability filter (`meetsNeed`), and the control loop has a third
copy that never marks a failing model unhealthy at all. Anything true of one is
not necessarily true of the others.

So `build_candidates()` is the only ranking function, and `need` is enforced here
rather than by each caller — the router used to have no concept of "can it
actually see an image", which is why that check had to be bolted on elsewhere and
was then missing from the third path entirely.

Three things changed at the switchover, and each is load-bearing.

**It ranks DEPLOYMENTS.** The pool is one model version reached through one
connection, so the same model offered by two providers is two candidates that
fail, cool down and get measured independently — which is what stops one
rate-limited reseller from taking a model offline on the key that still works.

**An unknown capability no longer reads as a missing one.** The filter excludes
on `Support.NO` and on nothing else. The old `caps` dict was booleans with no
third state, so "we have never established whether this model can see an image"
arrived as `False` and hid a capable model with no visible reason. A version
that has told us nothing is offered and allowed to fail honestly, which is the
only way anybody finds out.

**Role is a per-request classification, not a stored preference.** Each caller
tags its own `Task` with the role that best describes the work — a control-loop
step, a spoken reply, a one-off background ask — and `_score()` weighs the
ranking accordingly. There used to also be a persisted, per-role model/effort
pin here (`gateway/slots.py`); it was removed because it lived at the wrong
layer — a settings surface bolted into the ranking function rather than
something an application built on top of the gateway would own. `role` itself
stays: it is exactly the kind of per-request characteristic a gateway should
accept, and ten real callers rely on the distinction it draws.

Pure except for reading the deployment, availability, latency, price and
preference stores; no model calls, no network. `explain_exclusions()` walks the
SAME predicate, so a "nothing can answer this" message can never name a
different reason than the one that actually excluded the model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

from ..catalog import Capabilities, Lifecycle, Support, Version
from ..cost.advisor import observed_cost_tier
from . import availability, deployments, latency

#: A loose signal for "this needs real thinking" — enough to nudge the balance
#: dial, never enough to gate anything on its own.
_REASONING_HINTS = re.compile(
    r"\b(write|essay|code|debug|analy[sz]e|plan|compare|explain in depth|think through"
    r"|design|refactor|summar(y|ize)|research|draft)\b", re.I)

#: What a version's own `quality` reads as when nobody has set one, and what an
#: unmeasured price and an unmeasured latency read as. Every one of them is the
#: middle of its own scale, so an unknown model neither wins nor loses on a term
#: nothing has been established about — it is ranked on the terms that HAVE.
NEUTRAL_QUALITY = 3
NEUTRAL_COST = 2
NEUTRAL_SPEED = 3

#: The capabilities an UNKNOWN is not good enough for.
#:
#: The rule everywhere else is that only a definite NO excludes, because trying
#: a model nobody has asked about is the only way anybody finds out and the cost
#: of being wrong is one failed call. That reasoning depends on the failure
#: being VISIBLE. Send an image to a model that turns out to be blind and the
#: provider rejects it; the turn falls through to the next candidate and the
#: user is none the wiser.
#:
#: Web search is the one that does not work that way. A model that cannot search
#: does not refuse — it answers from memory, fluently, citing nothing, and the
#: reply is indistinguishable from one that really looked. There is no error to
#: fall through on, so "allowed to fail honestly" is not on offer: the only
#: honest outcome is to say nothing could look it up.
#:
#: Named per capability rather than per caller because it is a fact about the
#: capability. A second caller wanting a real search would otherwise have to
#: rediscover this, and would get it wrong the same way.
MUST_BE_CERTAIN = frozenset({"web_search"})


class Role(Enum):
    """A job a model gets asked to do.

    Each corresponds to something the ranking already treats differently, not
    to a category invented for the sake of having one. Purely a per-request
    classification a caller attaches to its own `Task` — there is no persisted,
    per-role model/effort override here; that was removed as an application-
    level concern that didn't belong inside the ranking function.
    """

    #: A person is waiting for the answer. The ordinary chat turn.
    CONVERSATION = "conversation"
    #: Spoken. Latency is most of the experience, so this is the one role where
    #: a faster, weaker model is often the RIGHT answer rather than a compromise.
    VOICE = "voice"
    #: Driving the screen. A wrong click costs more than a slightly clumsy
    #: sentence, and nobody is watching it happen in real time.
    CONTROL = "control"
    #: Scheduled tasks, job workers, briefings. Nobody is waiting, so cost
    #: matters more than latency.
    BACKGROUND = "background"
    #: The one-off asks — memory extraction, verification, heartbeat triage,
    #: improvement synthesis. Small, frequent, and answered in JSON.
    UTILITY = "utility"


def role_from(name: Any) -> Role:
    """A role from whatever a caller is holding, defaulting to conversation.

    The orchestrator names the role for a turn but may not import this module —
    the turn loop imports no part of the gateway — so it says "voice" or
    "control" as a plain string across the port and this is where that becomes
    a Role. An unrecognised name is not an error: a turn whose origin nobody
    classified is an ordinary conversation, and failing it over a label would
    be a routing decision made by a typo.
    """
    if isinstance(name, Role):
        return name
    try:
        return Role(str(name).strip().lower())
    except ValueError:
        return Role.CONVERSATION


@dataclass(frozen=True)
class Task:
    """What this turn needs, in routing terms.

    `role` replaced three overlapping fields — `source` ('text'/'voice'),
    `profile` ('chat'/'control') and a `background` boolean — which between them
    were one fact spelled three ways. Two of the three were set by no caller at
    all, so the control-loop scoring branch and the voice branch were both
    unreachable: every turn in the app arrived as a default text conversation
    however it had started. One field that every caller must pass is why that
    can no longer happen quietly.
    """

    text: str = ""
    role: Role = Role.CONVERSATION
    needs_tools: bool = True
    estimated_tokens: int = 0
    #: Hard requirements: vision / video / audio / webSearch.
    need: dict[str, bool] = field(default_factory=dict)

    @property
    def complexity(self) -> str:
        words = len(self.text.split())
        return "reasoning" if (_REASONING_HINTS.search(self.text) or words > 60) else "quick"

    @property
    def background(self) -> bool:
        """Whether nobody is waiting on this in real time.

        Derived rather than stored: it is exactly "the role is one of the two
        nobody watches", and a separate boolean beside the role could only ever
        disagree with it.
        """
        return self.role in (Role.BACKGROUND, Role.UTILITY)


#: What a deployment too damaged to resolve is treated as: everything unknown.
#: Not a special case in the predicate below — a record that cannot answer and
#: a model nobody has asked about are the same state, and writing them as one
#: is what stops a `MUST_BE_CERTAIN` need from being waved through by a missing
#: field.
UNRESOLVED = Version(provider="unknown", model="", label="")


def _version_of(entry: dict[str, Any]) -> Version:
    version = entry.get("version")
    return version if isinstance(version, Version) else UNRESOLVED


def _exclude_reason(entry: dict[str, Any], task: Task) -> str | None:
    """Why this deployment cannot serve this task, or None. The one predicate."""
    if not entry.get("enabled", True):
        return "disabled"
    if not deployments.is_ready(entry):
        return "needs_key"
    if not availability.is_eligible(entry["id"]):
        record = availability.status_of(entry["id"]) or {}
        return record.get("state") or "cooldown"

    version = _version_of(entry)
    if version.lifecycle is Lifecycle.RETIRED:
        # The provider has said this one is gone. Offering it means a failure
        # and a cooldown every few hours, forever, rediscovering the same dead
        # end — which is what the old build did, having no way to be told.
        return "retired"
    if task.needs_tools and version.capabilities.tools is Support.NO:
        return "no_tools"
    if (task.estimated_tokens and version.context_tokens
            and task.estimated_tokens > version.context_tokens):
        return "context_too_small"
    for capability, required in (task.need or {}).items():
        if not required:
            continue
        support = version.capabilities.get(capability)
        # Only a definite NO excludes — `UNKNOWN` is offered and allowed to fail
        # — except where a failure would be silent. See `MUST_BE_CERTAIN`.
        if support is Support.NO:
            return f"no_{capability}"
        if support is not Support.YES and _must_be_certain(capability):
            return f"unproven_{capability}"
    return None


def _must_be_certain(capability: str) -> bool:
    """Whether `UNKNOWN` is good enough for this one, under either spelling."""
    return Capabilities.normalise(capability) in MUST_BE_CERTAIN


def _quality_of(entry: dict[str, Any]) -> int:
    quality = _version_of(entry).quality
    return quality if isinstance(quality, int) else NEUTRAL_QUALITY


def _cost_of(entry: dict[str, Any]) -> int:
    """The measured price bucket, or neutral.

    The catalog deliberately ships no price: Jarvis records real spend, and a
    measured number beats an authored one. `None` means nothing has been
    recorded yet, and inventing a figure here would be a guess wearing the
    authority of a measurement.
    """
    measured = observed_cost_tier(deployments.provider_of(entry), entry.get("model"))
    return measured if measured is not None else NEUTRAL_COST


def _speed_of(entry: dict[str, Any]) -> int:
    measured = latency.speed_tier(entry["id"])
    return measured if measured is not None else NEUTRAL_SPEED


def _score(entry: dict[str, Any], task: Task, balance: str) -> float:
    """The weights are unchanged from before the switchover, on purpose.

    Every input now arrives from a different place — quality from the catalog,
    price from recorded spend, speed from recorded latency — but each lands in
    the same 1-5 domain the authored numbers used, so the spread this formula
    can produce is unchanged and `AVAILABILITY_BONUS` stays correctly sized
    without re-tuning anything.
    """
    speed = _speed_of(entry)
    quality = _quality_of(entry)
    cost = _cost_of(entry)

    if task.role is Role.CONTROL:
        # A wrong click costs more than a slightly-off sentence, and nobody is
        # waiting on this in real time. Vision is a preference, not a hard need.
        sighted = _version_of(entry).capabilities.vision is Support.YES
        return quality * 3 - cost * 0.5 + (3 if sighted else 0)
    if task.background:
        return quality * 2 - cost * 2
    if task.role is Role.VOICE:
        # Spoken. Latency is most of the experience, so a faster, weaker model
        # is often the right answer here rather than a compromise — which is
        # the whole reason the role is distinguished.
        return speed * 3 - cost * 0.5
    if balance == "fast":
        return speed * 3 - cost
    if balance == "quality":
        return quality * 3 - cost * 0.5
    if task.complexity == "reasoning":
        return quality * 2.5 - cost * 0.5
    return speed * 2 - cost


#: Sized to exceed the largest spread `_score` can produce across every branch,
#: so the ordering working > never-checked > known-bad is guaranteed rather than
#: merely likely. Without it a fast, cheap, DEAD model outranks the one model
#: actually known to work, and fills every fallback attempt with known failures.
AVAILABILITY_BONUS = 20


def _availability_score(entry: dict[str, Any]) -> float:
    record = availability.status_of(entry["id"])
    if record is None:
        return AVAILABILITY_BONUS       # nothing on record means nothing has failed
    return -AVAILABILITY_BONUS


def _balance() -> str:
    """The Fast / Balanced / Quality dial, read now rather than at startup.

    It used to be baked into the gateway when the orchestrator was constructed,
    which meant changing it did nothing until the process restarted — a setting
    that silently does not work until tomorrow.
    """
    from ..prefs import get_prefs

    try:
        return str(get_prefs().get("balance") or "balanced")
    except Exception:  # noqa: BLE001 — a ranking must not fail over a preference read
        return "balanced"


def build_candidates(
    task: Task,
    *,
    balance: str | None = None,
    model_id: str | None = None,
    entries: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Every deployment that could serve this task, best first.

    `model_id` is a one-off pin (a scheduled task naming its model) and beats
    pure ranking. A pin is honoured by ORDER, not by exclusion: if the pinned
    deployment fails mid-turn the rest of the list is still there, which is
    what keeps a pin from becoming a single point of failure.
    """
    pool = deployments.list_deployments() if entries is None else entries
    eligible = [e for e in pool if _exclude_reason(e, task) is None]
    chosen_balance = _balance() if balance is None else balance

    def sort_key(entry: dict[str, Any]) -> tuple:
        total = _score(entry, task, chosen_balance) + _availability_score(entry)
        # Ties broke on file order before this, so a free code-completion model
        # won every voice turn purely by being first in models.json.
        return (-total, -_quality_of(entry), entry.get("id", ""))

    ranked = sorted(eligible, key=sort_key)

    for pin in (model_id,):
        if not pin:
            continue
        pinned = next((e for e in ranked if e.get("id") == pin), None)
        if pinned is not None:
            ranked.remove(pinned)
            ranked.insert(0, pinned)
    return ranked


def explain_exclusions(task: Task, entries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Counts by reason plus the soonest retry, for a real "here's why" message."""
    pool = deployments.list_deployments() if entries is None else entries
    counts: dict[str, int] = {}
    soonest: int | None = None
    for entry in pool:
        reason = _exclude_reason(entry, task)
        if reason is None:
            continue
        counts[reason] = counts.get(reason, 0) + 1
        wait = availability.retry_after_ms(entry["id"])
        if wait > 0:
            soonest = wait if soonest is None else min(soonest, wait)
    return {"total": len(pool), "counts": counts, "soonestRetryMs": soonest}


def meets_need(entry: dict[str, Any], need: Iterable[str]) -> bool:
    """Whether a deployment DEFINITELY satisfies every named capability.

    Stricter than the router's own filter for most capabilities, and
    deliberately so: the filter asks "is there a reason not to offer this",
    where a caller reaching for this function is asking "can I rely on it".
    `UNKNOWN` answers the first question yes and the second no.
    """
    capabilities = _version_of(entry).capabilities
    return all(capabilities.get(item) is Support.YES for item in need)
