"""The Model Registry: every model made callable through a provider, and what
it can actually do.

A `Model` row is the crossing-axis unit — one model version reached through
one `Provider` — exactly the same shape as a deployment used to be, and for
the same reason: the same underlying model reachable through two providers
(your own key, and a reseller) is two independent rows here, each with its own
health, its own usage history, and its own place in the ranking, because a
rate limit on one route must never take the other one down with it.

**Effective metadata is a merge, computed at read time, never written down.**
§14/§15: "Provider metadata + Discovered metadata + User overrides = Effective
model metadata." Three JSON columns per mergeable field-group —
`capabilities_json` (this build's own small seed, matched by pattern),
`capabilities_discovered_json` (what the provider said), and
`capabilities_override_json` (what a person corrected) — are combined by
`hydrate()` on every read, with `USER > DISCOVERED > CATALOG` precedence per
field. Nothing is merged at write time, so a better catalog seed shipped
tomorrow, or a fresh discovery run, is reflected the next time anything reads
this model without a migration touching a single stored row.

**Nothing here invents a fact.** Where every source is silent, a field stays
`Support.UNKNOWN` / `None` and says so — filling a gap with a plausible
default is how a model with no recorded vision support ends up routed an
image.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Mapping, Pattern

from ..db import get_db
from ..jscompat import now_iso
from .capabilities import Capabilities, capabilities_from_dict, merge_capabilities
from .parameters import Param, merge_param_support, params_from_dict
from .providers import Provider, get_provider
from .reasoning import (
    NO_REASONING, ReasoningScheme, UNKNOWN_SCHEME, scheme_from_dict,
)

_lock = threading.RLock()

RESERVED_IDS = frozenset({"discover", "recheck", "health", "usage", "catalog"})


# --- the small seed this build ships knowing ---------------------------------
#
# Deliberately short, for the same reason the earlier catalog build's own seed
# was: every entry is a claim that ages, and a MISSING entry costs nothing (the
# model is simply unknown, which the rest of this system already handles
# correctly), while a WRONG one costs a model described incorrectly until
# someone notices. Keyed by pattern, not exact id — a provider's own listing
# returns dated snapshots (`claude-opus-5-20260115`), and an exact-match table
# would miss every one of them the day after it was written.

@dataclass(frozen=True)
class FamilySeed:
    family: str
    label: str
    pattern: Pattern[str]
    reasoning: ReasoningScheme
    capabilities: Capabilities = field(default_factory=Capabilities)
    quality: int | None = None
    #: Who MAKES this lineage — not necessarily the provider row a model is
    #: configured under. A model added under an aggregator/reseller provider
    #: is still serving this maker's model, with this maker's parameters and
    #: this maker's real cost — see `ResolvedModel.maker`.
    maker: str | None = None


def _tiers(native: dict, default) -> ReasoningScheme:
    from .reasoning import Effort, ReasoningKind
    return ReasoningScheme(kind=ReasoningKind.TIERS, levels=tuple(sorted(native)),
                           default=default, native=dict(native))


def _budget(default) -> ReasoningScheme:
    from .reasoning import Effort, ReasoningKind
    native = {Effort.OFF: 0, Effort.LOW: 4096, Effort.MEDIUM: 16384,
              Effort.HIGH: 32768, Effort.MAXIMUM: 65536}
    return ReasoningScheme(kind=ReasoningKind.BUDGET, levels=tuple(sorted(native)),
                           default=default, native=native)


def _seed() -> tuple[FamilySeed, ...]:
    from .capabilities import Support
    from .reasoning import Effort

    gemini_native = {Effort.LOW: "LOW", Effort.MEDIUM: "MEDIUM", Effort.HIGH: "HIGH"}
    gpt_native = {Effort.OFF: "none", Effort.LOW: "low", Effort.MEDIUM: "medium",
                  Effort.HIGH: "high", Effort.MAXIMUM: "max"}
    tool_caps = Capabilities(tool_calling=Support.YES, parallel_tool_calls=Support.YES,
                              streaming=Support.YES)
    return (
        FamilySeed("claude-opus", "Claude Opus", re.compile(r"(^|/)claude-opus[-.]", re.I),
                   _budget(Effort.MEDIUM), tool_caps, quality=5, maker="anthropic"),
        FamilySeed("claude-sonnet", "Claude Sonnet", re.compile(r"(^|/)claude-sonnet[-.]", re.I),
                   _budget(Effort.MEDIUM), tool_caps, quality=4, maker="anthropic"),
        FamilySeed("claude-haiku", "Claude Haiku", re.compile(r"(^|/)claude-haiku[-.]", re.I),
                   _budget(Effort.LOW), tool_caps, quality=3, maker="anthropic"),
        FamilySeed("gemini-pro", "Gemini Pro", re.compile(r"(^|/)gemini[-.\d]*-pro", re.I),
                   _tiers(gemini_native, Effort.MEDIUM), tool_caps, quality=5, maker="google"),
        FamilySeed("gemini-flash", "Gemini Flash", re.compile(r"(^|/)gemini[-.\d]*-flash", re.I),
                   _tiers(gemini_native, Effort.LOW), tool_caps, quality=3, maker="google"),
        FamilySeed("gpt", "GPT", re.compile(r"(^|/)gpt-", re.I),
                   _tiers(gpt_native, Effort.MEDIUM), tool_caps, quality=4, maker="openai"),
    )


SEED: tuple[FamilySeed, ...] = _seed()


def match_seed(native_model_id: str, rules: tuple[FamilySeed, ...] = SEED) -> FamilySeed | None:
    for rule in rules:
        if rule.pattern.search(native_model_id or ""):
            return rule
    return None


_DATED = re.compile(r"[-_@](20\d{2}[-_]?\d{2}[-_]?\d{2}|20\d{2}[-_]?\d{2})$")


def looks_pinned(native_model_id: str) -> bool:
    return bool(_DATED.search(native_model_id or ""))


# --- the resolved, effective view of one model -------------------------------

@dataclass(frozen=True)
class ResolvedModel:
    """Everything known about one model, merged from catalog + discovered +
    user override, with the provider it's reached through joined in."""

    id: str
    provider: Provider
    native_model_id: str
    display_name: str
    family: str | None
    #: Who MAKES this model — not necessarily `provider.id`. A model added
    #: under an aggregator/reseller provider is still serving its real
    #: maker's model, with that maker's real cost; this is the key spend and
    #: pricing are filed under. Falls back to the provider's own id when the
    #: maker is not recognised, the same fallback `provider.id` would give.
    maker: str
    version_label: str | None
    status: str
    context_window: int | None
    max_input_tokens: int | None
    max_output_tokens: int | None
    capabilities: Capabilities
    parameters: Mapping[Param, Any]
    reasoning: ReasoningScheme
    pricing: Mapping[str, Any] | None
    quality: int | None
    enabled: bool
    notes: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "providerId": self.provider.id, "maker": self.maker,
            "provider": self.provider.as_dict(),
            "nativeModelId": self.native_model_id,
            "displayName": self.display_name, "family": self.family,
            "versionLabel": self.version_label, "status": self.status,
            "limits": {"contextWindow": self.context_window,
                       "maxInputTokens": self.max_input_tokens,
                       "maxOutputTokens": self.max_output_tokens},
            "capabilities": self.capabilities.as_dict(),
            "supportedParams": {p.value: s.value for p, s in self.parameters.items()},
            "reasoning": self.reasoning.as_dict(),
            "pricing": dict(self.pricing) if self.pricing else None,
            "quality": self.quality, "enabled": self.enabled, "notes": self.notes,
        }


def _row_to_resolved(row: Any) -> ResolvedModel | None:
    provider = get_provider(row["provider_id"])
    if provider is None:
        return None
    seed = match_seed(row["native_model_id"])
    seed_caps = seed.capabilities if seed else Capabilities()
    capabilities = merge_capabilities(
        json.loads(row["capabilities_override_json"] or "{}"),
        json.loads(row["capabilities_discovered_json"] or "{}"),
        seed_caps,
    )
    parameters = merge_param_support(
        params_from_dict(json.loads(row["parameters_json"] or "{}")),
    )
    reasoning = (
        scheme_from_dict(json.loads(row["reasoning_override_json"] or "{}"))
        or scheme_from_dict(json.loads(row["reasoning_discovered_json"] or "{}"))
        or (seed.reasoning if seed else None)
        or UNKNOWN_SCHEME
    )
    pricing = json.loads(row["pricing_json"]) if row["pricing_json"] else None
    quality = row["quality"] if row["quality"] is not None else (seed.quality if seed else None)
    family = row["family"] or (seed.family if seed else None)
    maker = (seed.maker if seed and seed.maker else None) or provider.id
    display = row["label"] or row["display_name"] or row["native_model_id"]

    return ResolvedModel(
        id=row["id"], provider=provider, native_model_id=row["native_model_id"],
        display_name=display, family=family, maker=maker, version_label=row["version_label"],
        status=row["status"], context_window=row["context_window"],
        max_input_tokens=row["max_input_tokens"], max_output_tokens=row["max_output_tokens"],
        capabilities=capabilities, parameters=parameters, reasoning=reasoning,
        pricing=pricing, quality=quality, enabled=bool(row["enabled"]),
        notes=row["notes"] or "",
    )


def list_models(provider_id: str | None = None) -> list[ResolvedModel]:
    db = get_db()
    if provider_id:
        rows = db.execute(
            "SELECT * FROM ai_models WHERE provider_id = ? ORDER BY created_at ASC",
            (provider_id,)).fetchall()
    else:
        rows = db.execute("SELECT * FROM ai_models ORDER BY created_at ASC").fetchall()
    out = []
    for row in rows:
        resolved = _row_to_resolved(row)
        if resolved is not None:
            out.append(resolved)
    return out


def get_model(model_id: str) -> ResolvedModel | None:
    row = get_db().execute("SELECT * FROM ai_models WHERE id = ?", (model_id,)).fetchone()
    return _row_to_resolved(row) if row is not None else None


def _slug(seed: str, fallback: str = "model") -> str:
    base = re.sub(r"(^-|-$)", "", re.sub(r"[^a-z0-9]+", "-", str(seed or fallback).lower().strip()))
    return base or fallback


def _unique_id(seed: str, existing: set[str]) -> str:
    base = _slug(seed)
    taken = existing | RESERVED_IDS
    candidate, n = base, 2
    while candidate in taken:
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def add_model(
    *,
    provider_id: str,
    native_model_id: str,
    label: str | None = None,
    discovered: dict[str, Any] | None = None,
    quality: int | None = None,
    notes: str = "",
) -> ResolvedModel:
    if not provider_id or not native_model_id:
        raise ValueError("A model needs a provider and a model id.")
    if get_provider(provider_id) is None:
        raise KeyError(f"Unknown provider: {provider_id}")
    discovered = discovered or {}

    with _lock:
        db = get_db()
        dupe = db.execute(
            "SELECT id FROM ai_models WHERE provider_id = ? AND native_model_id = ?",
            (provider_id, native_model_id)).fetchone()
        if dupe is not None:
            raise ValueError(f'"{native_model_id}" is already added under this provider.')
        existing = {r["id"] for r in db.execute("SELECT id FROM ai_models").fetchall()}
        model_id = _unique_id(label or native_model_id, existing)
        db.execute(
            "INSERT INTO ai_models (id, provider_id, native_model_id, display_name, label, "
            "status, context_window, max_input_tokens, max_output_tokens, "
            "capabilities_discovered_json, parameters_json, reasoning_discovered_json, "
            "quality, enabled, notes, discovered_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'unknown', ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
            (model_id, provider_id, native_model_id, native_model_id,
             label if label and label != native_model_id else None,
             discovered.get("contextWindow"), discovered.get("maxInputTokens"),
             discovered.get("maxOutputTokens"),
             json.dumps(discovered.get("capabilities") or {}),
             json.dumps(discovered.get("supportedParams") or {}),
             json.dumps(discovered.get("reasoning") or {}),
             quality, notes, now_iso(), now_iso()),
        )
        return get_model(model_id)  # type: ignore[return-value]


PATCH_FIELDS = {"label", "enabled", "notes", "capability_overrides", "reasoning_override", "quality"}


def update_model(model_id: str, patch: dict[str, Any]) -> ResolvedModel:
    with _lock:
        db = get_db()
        if get_model(model_id) is None:
            raise KeyError(f"Unknown model: {model_id}")
        sets: list[str] = []
        values: list[Any] = []
        if "label" in patch:
            sets.append("label = ?"); values.append(patch["label"])
        if "enabled" in patch:
            sets.append("enabled = ?"); values.append(int(bool(patch["enabled"])))
        if "notes" in patch:
            sets.append("notes = ?"); values.append(patch["notes"])
        if "quality" in patch:
            sets.append("quality = ?"); values.append(patch["quality"])
        if "capability_overrides" in patch:
            sets.append("capabilities_override_json = ?")
            values.append(json.dumps(patch["capability_overrides"] or {}))
        if "reasoning_override" in patch:
            sets.append("reasoning_override_json = ?")
            values.append(json.dumps(patch["reasoning_override"] or {}))
        if sets:
            values.append(model_id)
            db.execute(f"UPDATE ai_models SET {', '.join(sets)} WHERE id = ?", values)
        return get_model(model_id)  # type: ignore[return-value]


def record_discovery(model_id: str, facts: dict[str, Any]) -> ResolvedModel | None:
    """Merge in what a provider just reported about this model — never what a
    person said (that is `capability_overrides`/`reasoning_override` via
    `update_model`). Kept as its own write path for the same reason the
    earlier build kept `discovered` and `overrides` apart: two different
    authorities, and a single patch surface would let one silently overwrite
    the other.
    """
    with _lock:
        db = get_db()
        row = db.execute("SELECT * FROM ai_models WHERE id = ?", (model_id,)).fetchone()
        if row is None:
            return None
        sets: list[str] = ["discovered_at = ?"]
        values: list[Any] = [now_iso()]
        if "status" in facts:
            sets.append("status = ?"); values.append(facts["status"])
        if "contextWindow" in facts:
            sets.append("context_window = ?"); values.append(facts["contextWindow"])
        if "maxInputTokens" in facts:
            sets.append("max_input_tokens = ?"); values.append(facts["maxInputTokens"])
        if "maxOutputTokens" in facts:
            sets.append("max_output_tokens = ?"); values.append(facts["maxOutputTokens"])
        if "displayName" in facts:
            sets.append("display_name = ?"); values.append(facts["displayName"])
        if "capabilities" in facts:
            merged = {**json.loads(row["capabilities_discovered_json"] or "{}"),
                      **facts["capabilities"]}
            sets.append("capabilities_discovered_json = ?"); values.append(json.dumps(merged))
        if "supportedParams" in facts:
            merged = {**json.loads(row["parameters_json"] or "{}"), **facts["supportedParams"]}
            sets.append("parameters_json = ?"); values.append(json.dumps(merged))
        if "reasoning" in facts:
            sets.append("reasoning_discovered_json = ?"); values.append(json.dumps(facts["reasoning"]))
        if "pricing" in facts:
            sets.append("pricing_json = ?"); values.append(json.dumps(facts["pricing"]))
        values.append(model_id)
        db.execute(f"UPDATE ai_models SET {', '.join(sets)} WHERE id = ?", values)
        return get_model(model_id)


def record_parameter_refusal(model_id: str, param: Param) -> None:
    """A live call just told us this model rejects this parameter — recorded
    as a discovered `NO`, so the next request omits it instead of paying for
    the same rejection again. See `model_system/fallback.py`."""
    record_discovery(model_id, {"supportedParams": {param.value: "no"}})


def delete_model(model_id: str) -> None:
    with _lock:
        db = get_db()
        db.execute("DELETE FROM ai_models WHERE id = ?", (model_id,))
        db.execute("DELETE FROM ai_health WHERE model_id = ?", (model_id,))
