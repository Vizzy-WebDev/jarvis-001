"""Model routing (§9-11): requirements eliminate, preferences rank.

**A cheap text-only model must never win an image request simply because it
is cheap.** That is the one sentence this whole module exists to make true.
`_exclude()` answers "can this model even attempt this" — a hard yes/no — and
only once every incompatible model has been removed does `_score()` get to
express an opinion about which of the survivors is best. Swapping that
order, or letting a high enough score override an exclusion, is the one
mistake this file cannot make.

**Only a confirmed `NO` excludes a capability requirement.** A model nobody
has asked about is offered and allowed to fail honestly — trying is the only
way anyone finds out, and the cost of being wrong is one failed call that
`ai/fallback.py` moves past. `MUST_BE_CERTAIN` names the one exception: web
search's absence fails SILENTLY (a model that cannot search answers from
memory, fluently, citing nothing), so `UNKNOWN` is not good enough there.

This module implements exactly one ranking strategy, deterministic and
transparent (§11 calls this acceptable for a first implementation and asks
for the shape to stay replaceable — `rank()` takes `preferences` as data
rather than being hard-coded to one formula, which is the seam a second
strategy would plug into; nothing here justifies a class hierarchy of
router objects before a second strategy actually exists to put in one).
"""

from __future__ import annotations

from typing import Any

from . import health
from . import usage as usage_store
from .capabilities import Capabilities, Support
from .credentials import status_of as credential_status_of
from .credentials import CredentialStatus
from .registry import ResolvedModel, list_models
from .request import Modality, Preferences, Requirements, Role

#: A requirement's input modality -> the capability that answers it. `FILE`
#: and `TEXT` are absent: text is universal, and "accepts a file" is not one
#: fact but whichever of vision/audio/video actually applies to its content.
MODALITY_CAPABILITY: dict[Modality, str] = {
    Modality.IMAGE: "vision",
    Modality.AUDIO: "audio_input",
    Modality.VIDEO: "video_input",
}

#: The capabilities an UNKNOWN is not good enough for — see this module's
#: docstring. Named per capability, not per caller, so a second caller
#: wanting a real search does not have to rediscover this.
MUST_BE_CERTAIN = frozenset({"web_search"})

NEUTRAL_QUALITY = 3
NEUTRAL_COST = 2
#: What an unmeasured speed reads as — the middle of the scale, so a model
#: nothing has been measured about neither wins nor loses on a term nothing
#: is known about yet. `ai/usage.py`'s `speed_tier()` supplies the real,
#: measured value once enough calls have completed.
NEUTRAL_SPEED = 3

#: USD per 1,000 tokens, blended across input and output — a coarse bucket
#: for ranking, not a cost estimate (that lives in a usage report).
_COST_BUCKETS: tuple[tuple[float, int], ...] = ((0.0005, 0), (0.002, 1), (0.01, 2), (0.03, 3))

#: Sized to exceed the largest spread `_score()` can produce across every
#: branch, so "never failed" always outranks "recently failed but past its
#: cooldown" regardless of any other preference in play.
AVAILABILITY_BONUS = 20
PREFERRED_MODEL_BONUS = 50
PREFERRED_PROVIDER_BONUS = 1.0


def _must_be_certain(name: str) -> bool:
    """Whether `UNKNOWN` is not good enough for this one, under any of the
    spellings a caller might use — normalised first, or a differently-spelled
    requirement (`webSearch` rather than `web_search`) would silently fail to
    match `MUST_BE_CERTAIN` and be treated as an ordinary, offer-and-allow
    capability instead."""
    return Capabilities.normalise(name) in MUST_BE_CERTAIN


def _exclude(model: ResolvedModel, requirements: Requirements) -> str | None:
    """Why this model cannot serve this request, or `None`. The one
    predicate — `explain_exclusions()` walks the SAME function, so a "nothing
    can answer this" message can never name a different reason than the one
    that actually excluded a candidate."""
    if not model.enabled:
        return "disabled"
    if not model.provider.enabled:
        return "provider_disabled"
    if credential_status_of(model.provider.credential_ref,
                            key_required=model.provider.key_required) is CredentialStatus.NOT_CONFIGURED:
        return "needs_key"
    if not health.is_eligible(model.id):
        record = health.status_of(model.id) or {}
        return record.get("state") or "cooling_down"
    if model.status == "retired":
        return "retired"

    if requirements.needs_tools and model.capabilities.tool_calling is Support.NO:
        return "no_tool_calling"
    if requirements.needs_structured_output and model.capabilities.structured_output is Support.NO:
        return "no_structured_output"
    if requirements.needs_streaming and model.capabilities.streaming is Support.NO:
        return "no_streaming"
    if (requirements.min_context_tokens and model.context_window
            and requirements.min_context_tokens > model.context_window):
        return "context_too_small"

    for modality in requirements.modalities_in:
        capability = MODALITY_CAPABILITY.get(modality)
        if capability is None:
            continue
        support = model.capabilities.get(capability)
        if support is Support.NO:
            return f"no_{capability}"
        if support is not Support.YES and _must_be_certain(capability):
            return f"unproven_{capability}"

    for name, required in (requirements.capabilities or {}).items():
        if not required:
            continue
        support = model.capabilities.get(name)
        if support is Support.NO:
            return f"no_{name}"
        if support is not Support.YES and _must_be_certain(name):
            return f"unproven_{name}"
    return None


def _cost_tier(model: ResolvedModel) -> int:
    """The measured/declared price bucket, or neutral. Pricing lives on the
    model as metadata (§16) — never a name-based guess, and never routing
    logic hard-coded into source.

    Falls back to the shared price ledger (`cost/prices.py`) when the model's
    own record carries none: that ledger is what `seed_known_free_prices()`
    and `refresh_from_openrouter()` actually write to, and it is keyed on the
    same `(maker, native_model_id)` pair `ai/usage.py` and every cost event
    already use — one ledger, read by both the spend report and the router,
    rather than two that could disagree.
    """
    pricing = dict(model.pricing or {})
    if not pricing:
        from ..cost import store as cost_store
        found = cost_store.get_price(model.maker, model.native_model_id, "tokens")
        if found is not None:
            price_in = found.get("priceIn")
            price_out = found.get("priceOut")
            pricing = {
                "inputPer1k": price_in * 1000 if isinstance(price_in, (int, float)) else None,
                "outputPer1k": price_out * 1000 if isinstance(price_out, (int, float)) else None,
            }
    price_in = pricing.get("inputPer1k")
    price_out = pricing.get("outputPer1k")
    if price_in is None and price_out is None:
        return NEUTRAL_COST
    blended = ((price_in or 0.0) + (price_out or 0.0)) / 2
    for ceiling, tier in _COST_BUCKETS:
        if blended <= ceiling:
            return tier
    return 4


def _quality_of(model: ResolvedModel) -> int:
    return model.quality if isinstance(model.quality, int) else NEUTRAL_QUALITY


def _speed_of(model: ResolvedModel) -> int:
    measured = usage_store.speed_tier(model.id)
    return measured if measured is not None else NEUTRAL_SPEED


def _score(model: ResolvedModel, preferences: Preferences, balance: str) -> float:
    quality = _quality_of(model)
    cost = _cost_tier(model)
    speed = _speed_of(model)

    if preferences.role is Role.CONTROL:
        sighted = model.capabilities.vision is Support.YES
        base = quality * 3 - cost * 0.5 + (3 if sighted else 0)
    elif preferences.role.background_work:
        base = quality * 2 - cost * 2
    elif preferences.role is Role.VOICE:
        base = speed * 3 - cost * 0.5
    elif balance == "fast":
        base = speed * 3 - cost
    elif balance == "quality":
        base = quality * 3 - cost * 0.5
    else:
        base = speed * 2 - cost

    if preferences.preferred_provider_id and model.provider.id == preferences.preferred_provider_id:
        base += PREFERRED_PROVIDER_BONUS
    return base


def _availability_score(model: ResolvedModel) -> float:
    return AVAILABILITY_BONUS if health.status_of(model.id) is None else -AVAILABILITY_BONUS


def _balance() -> str:
    """The Fast/Balanced/Quality dial, read per call rather than cached at
    construction — a setting baked in once would silently stop working until
    the process restarted."""
    from ..prefs import get_prefs

    try:
        return str(get_prefs().get("balance") or "balanced")
    except Exception:  # noqa: BLE001 — a ranking must never fail over a preference read
        return "balanced"


def rank(
    requirements: Requirements,
    preferences: Preferences,
    *,
    balance: str | None = None,
    models: list[ResolvedModel] | None = None,
) -> list[ResolvedModel]:
    """Every model that could serve this request, best first.

    `preferences.preferred_model_id` is honoured by ORDER, not by exclusion —
    moved to the front if it is eligible, left out entirely otherwise. A
    preference that could remove every other candidate would not be a
    preference.
    """
    pool = list_models() if models is None else models
    eligible = [m for m in pool if _exclude(m, requirements) is None]
    chosen_balance = _balance() if balance is None else balance

    def sort_key(model: ResolvedModel) -> tuple:
        total = _score(model, preferences, chosen_balance) + _availability_score(model)
        return (-total, -_quality_of(model), model.id)

    ranked = sorted(eligible, key=sort_key)

    preferred = preferences.preferred_model_id
    if preferred:
        match = next((m for m in ranked if m.id == preferred), None)
        if match is not None:
            ranked.remove(match)
            ranked.insert(0, match)
    return ranked


def explain_exclusions(
    requirements: Requirements, models: list[ResolvedModel] | None = None,
) -> dict[str, Any]:
    """Counts by reason plus the soonest retry — for a real "here's why"
    message rather than a generic apology (§30)."""
    pool = list_models() if models is None else models
    counts: dict[str, int] = {}
    soonest: int | None = None
    for model in pool:
        reason = _exclude(model, requirements)
        if reason is None:
            continue
        counts[reason] = counts.get(reason, 0) + 1
        wait = health.retry_after_ms(model.id)
        if wait > 0:
            soonest = wait if soonest is None else min(soonest, wait)
    return {"total": len(pool), "counts": counts, "soonestRetryMs": soonest}


def select(
    requirements: Requirements, preferences: Preferences, *, balance: str | None = None,
) -> ResolvedModel | None:
    """The single best candidate — Auto's answer to "which model." `None`
    means nothing can serve this request at all."""
    ranked = rank(requirements, preferences, balance=balance)
    return ranked[0] if ranked else None
