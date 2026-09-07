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

Pure except for reading the registry and the availability store; no model calls,
no network. `explain_exclusions()` walks the SAME predicate, so a "nothing can
answer this" message can never name a different reason than the one that actually
excluded the model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..cost.advisor import observed_cost_tier
from . import availability
from .registry import is_ready, list_models

#: A loose signal for "this needs real thinking" — enough to nudge the balance
#: dial, never enough to gate anything on its own.
_REASONING_HINTS = re.compile(
    r"\b(write|essay|code|debug|analy[sz]e|plan|compare|explain in depth|think through"
    r"|design|refactor|summar(y|ize)|research|draft)\b", re.I)


@dataclass(frozen=True)
class Task:
    """What this turn needs, in routing terms."""

    text: str = ""
    source: str = "text"                 # 'text' | 'voice'
    background: bool = False
    profile: str = "chat"                # 'chat' | 'control'
    needs_tools: bool = True
    estimated_tokens: int = 0
    #: Hard requirements: vision / video / audio / webSearch.
    need: dict[str, bool] = field(default_factory=dict)

    @property
    def complexity(self) -> str:
        words = len(self.text.split())
        return "reasoning" if (_REASONING_HINTS.search(self.text) or words > 60) else "quick"


def _exclude_reason(entry: dict[str, Any], task: Task) -> str | None:
    """Why this model cannot serve this task, or None. The one predicate."""
    if not entry.get("enabled", True):
        return "disabled"
    if not is_ready(entry):
        return "needs_key"
    if not availability.is_eligible(entry["id"]):
        record = availability.status_of(entry["id"]) or {}
        return record.get("state") or "cooldown"
    caps = entry.get("caps") or {}
    if task.needs_tools and not caps.get("tools"):
        return "no_tools"
    context = caps.get("contextTokens")
    if task.estimated_tokens and context and task.estimated_tokens > context:
        return "context_too_small"
    for capability, required in (task.need or {}).items():
        if required and not caps.get(capability):
            return f"no_{capability}"
    return None


def _score(entry: dict[str, Any], task: Task, balance: str) -> float:
    tier = entry.get("tier") or {}
    speed = tier.get("speed", 3)
    quality = tier.get("quality", 3)
    # A price actually on record beats the catalog's name-regex guess. Same 0-4
    # domain, so every weight below stays exactly as tuned; None means nothing
    # has been measured yet and the guess stands.
    measured = observed_cost_tier(entry.get("provider") or entry.get("adapter"),
                                  entry.get("model"))
    cost = measured if measured is not None else tier.get("cost", 2)
    caps = entry.get("caps") or {}

    if task.profile == "control":
        # A wrong click costs more than a slightly-off sentence, and nobody is
        # waiting on this in real time. Vision is a preference, not a hard need.
        return quality * 3 - cost * 0.5 + (3 if caps.get("vision") else 0)
    if task.background:
        return quality * 2 - cost * 2
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


def build_candidates(
    task: Task,
    *,
    balance: str = "balanced",
    model_id: str | None = None,
    manual_model_id: str | None = None,
    entries: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Every model that could serve this task, best first.

    `model_id` is a one-off pin (a scheduled task naming its model) and beats the
    user's global manual pick, which beats pure ranking. A pin is honoured by
    ORDER, not by exclusion: if the pinned model fails mid-turn the rest of the
    list is still there, which is what keeps a pin from becoming a single point
    of failure.
    """
    pool = list_models() if entries is None else entries
    eligible = [e for e in pool if _exclude_reason(e, task) is None]

    def sort_key(entry: dict[str, Any]) -> tuple:
        total = _score(entry, task, balance) + _availability_score(entry)
        tier = entry.get("tier") or {}
        # Ties broke on file order before this, so a free code-completion model
        # won every voice turn purely by being first in models.json.
        return (-total, -(tier.get("quality", 3)), entry.get("id", ""))

    ranked = sorted(eligible, key=sort_key)

    for pin in (manual_model_id, model_id):
        if not pin:
            continue
        pinned = next((e for e in ranked if e.get("id") == pin), None)
        if pinned is not None:
            ranked.remove(pinned)
            ranked.insert(0, pinned)
    return ranked


def explain_exclusions(task: Task, entries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Counts by reason plus the soonest retry, for a real "here's why" message."""
    pool = list_models() if entries is None else entries
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
    caps = entry.get("caps") or {}
    return all(caps.get(item) for item in need)
