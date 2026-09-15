"""Asking a provider what it has, and reconciling that with what is
configured (§14-15).

Discovery is the authority on one thing only: what EXISTS. What those models
ARE is `ai/registry.py`'s job, merged per field with `USER > DISCOVERED >
CATALOG` precedence — a provider reporting its own model's context window is
fact in a way a pattern match never is, and this module's whole output feeds
straight into that merge as the DISCOVERED layer.

**Nothing here deletes a model.** A model vanishing from a listing is usually
retirement, but it is also what a lost API key, a changed plan, or a
provider having a bad morning look like. Marking it `retired` is reversible;
silent deletion is not. A model that comes back is simply listed again and
the mark is lifted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..redact import redact
from .providers import Provider, get_provider
from .reasoning import Effort, ReasoningKind, ReasoningScheme
from .registry import ResolvedModel, list_models, record_discovery

logger = logging.getLogger(__name__)

#: Parameter names that mean "this model can be asked to reason." Spellings
#: differ between gateways, so several are recognised.
_REASONING_PARAMETERS = frozenset({
    "reasoning", "reasoning_effort", "include_reasoning", "thinking",
    "thinking_config", "thinking_budget", "thinking_level",
})

#: The ladder assumed when a provider says reasoning is accepted but not
#: which levels. Conservative on purpose — stops at HIGH rather than
#: claiming MAXIMUM, because "this model takes a reasoning parameter" does
#: not say how far it goes, and a clamp costs nothing while an over-claim
#: costs a failed call.
_DISCOVERED_TIERS = ReasoningScheme(
    kind=ReasoningKind.TIERS, levels=(Effort.LOW, Effort.MEDIUM, Effort.HIGH),
    default=Effort.MEDIUM,
    native={Effort.LOW: "low", Effort.MEDIUM: "medium", Effort.HIGH: "high"},
)

#: Capability names a listing may report, mapped onto this system's own.
_CAPABILITY_ALIASES = {
    "vision": "vision", "image": "vision", "images": "vision",
    "video": "video_input", "audio": "audio_input",
    "tools": "tool_calling", "tool_use": "tool_calling", "function_calling": "tool_calling",
    "parallel_tool_calls": "parallel_tool_calls",
    "web_search": "web_search", "search": "web_search",
    "structured_output": "structured_output", "json_mode": "structured_output",
    "embeddings": "embeddings", "computer_use": "computer_use",
}


@dataclass(frozen=True)
class Listed:
    """One model a provider says it has, already in registry terms."""

    native_model_id: str
    discovered: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Present:
    """A configured model the provider still lists, and what it said."""

    model_id: str
    listed: Listed


@dataclass(frozen=True)
class Reconciliation:
    provider_id: str
    added: tuple[Listed, ...] = ()
    present: tuple[Present, ...] = ()
    retired: tuple[str, ...] = ()
    error: str | None = None

    @property
    def reachable(self) -> bool:
        return self.error is None


def _capabilities_from(reported: Any) -> dict[str, bool]:
    out: dict[str, bool] = {}
    if not isinstance(reported, Iterable) or isinstance(reported, (str, bytes)):
        return out
    for item in reported:
        name = _CAPABILITY_ALIASES.get(str(item).strip().lower())
        if name:
            out[name] = True
    return out


def reasoning_from_parameters(reported: Any) -> ReasoningScheme | None:
    """What a supported-parameters list says about reasoning.

    Three outcomes: not reported at all -> `None` (the registry's other
    sources stand); reported and includes a reasoning parameter -> a real
    scheme; reported and does NOT -> `ReasoningKind.NONE`, a DISCOVERED fact
    rather than an assumption, and the only way to learn it without spending
    a rejected request to find out.
    """
    if not isinstance(reported, Iterable) or isinstance(reported, (str, bytes)):
        return None
    names = {str(item).strip().lower() for item in reported}
    if not names:
        return None
    if names & _REASONING_PARAMETERS:
        return _DISCOVERED_TIERS
    return ReasoningScheme(kind=ReasoningKind.NONE)


def normalise(raw: Iterable[dict[str, Any]] | None) -> list[Listed]:
    """An adapter's own `discover_models()` rows, translated into registry
    terms. Adapters answer in their own vocabulary; this is the one place
    three wire formats become one."""
    out: list[Listed] = []
    for row in raw or ():
        if not isinstance(row, dict):
            continue
        model = str(row.get("model") or "").strip()
        if not model:
            continue

        facts: dict[str, Any] = {"status": "current"}
        if row.get("label"):
            facts["displayName"] = row["label"]
        context = row.get("contextTokens", row.get("context_tokens"))
        if context:
            facts["contextWindow"] = int(context)

        capabilities = _capabilities_from(row.get("capabilities"))
        if capabilities:
            facts["capabilities"] = capabilities

        scheme = reasoning_from_parameters(row.get("supported_parameters", row.get("supportedParameters")))
        if scheme is not None:
            facts["reasoning"] = scheme.as_dict()

        out.append(Listed(native_model_id=model, discovered=facts))
    return out


def for_picker(rows: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """A provider's listing as the add-a-model picker shows it — the family a
    model belongs to, guessed the same way the registry itself would, so the
    picker and the registry can never disagree about grouping."""
    from .registry import match_seed

    out: list[dict[str, Any]] = []
    for raw in rows or ():
        item = {"model": raw} if isinstance(raw, str) else dict(raw)
        if not item.get("model"):
            continue
        item.setdefault("label", item["model"])
        item.setdefault("contextTokens", None)
        seed = match_seed(str(item["model"]))
        item["family"] = seed.family if seed else None
        out.append(item)
    return out


def fetch(provider_id: str) -> tuple[list[Listed], str | None]:
    """What this provider says it has. Answers `(models, error)` rather than
    an empty list either way — "reached it, found nothing" and "could not
    reach it" are different problems with different fixes."""
    from .adapters import get_adapter

    provider = get_provider(provider_id)
    if provider is None:
        return [], "That provider no longer exists."

    try:
        module = get_adapter(provider.adapter)
    except KeyError:
        return [], "No adapter is registered for this provider's wire format."

    try:
        found = module.discover_models(provider)
    except Exception as err:  # noqa: BLE001 — an unreachable address is an answer
        logger.info("discovery failed for %s: %s", provider_id, redact(str(err)))
        try:
            message = module.friendly_error(err)
        except Exception:  # noqa: BLE001
            message = str(err)
        return [], redact(message) or "Could not reach that provider."

    return normalise(found), None


def reconcile(provider_id: str, listed: Iterable[Listed]) -> Reconciliation:
    """Compare a fresh listing against what is configured. Pure — decides
    nothing, writes nothing, so a caller can preview before anything changes."""
    configured = [m for m in list_models(provider_id)]
    by_native = {m.native_model_id: m for m in configured}
    listed = list(listed)
    by_name = {row.native_model_id: row for row in listed}

    return Reconciliation(
        provider_id=provider_id,
        added=tuple(row for row in listed if row.native_model_id not in by_native),
        present=tuple(Present(model_id=m.id, listed=by_name[m.native_model_id])
                     for m in configured if m.native_model_id in by_name),
        retired=tuple(m.id for m in configured if m.native_model_id not in by_name),
    )


def apply(reconciliation: Reconciliation) -> dict[str, int]:
    """Write what the listing established, and nothing else. Adding models in
    `added` is left to the caller — discovering four hundred models is not
    consent to configure four hundred models."""
    if not reconciliation.reachable:
        return {"retired": 0, "restored": 0, "refreshed": 0}

    restored = refreshed = 0
    for row in reconciliation.present:
        current = next((m for m in list_models() if m.id == row.model_id), None)
        if current is None:
            continue
        if current.status == "retired":
            restored += 1
        record_discovery(row.model_id, {**row.listed.discovered, "status": "current"})
        refreshed += 1

    retired = 0
    for model_id in reconciliation.retired:
        if record_discovery(model_id, {"status": "retired"}) is not None:
            retired += 1

    return {"retired": retired, "restored": restored, "refreshed": refreshed}


def refresh(provider_id: str) -> Reconciliation:
    """Fetch, reconcile and record, in one call."""
    listed, error = fetch(provider_id)
    if error is not None:
        return Reconciliation(provider_id=provider_id, error=error)
    result = reconcile(provider_id, listed)
    apply(result)
    return result
