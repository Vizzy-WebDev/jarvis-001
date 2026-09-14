"""Asking a provider what it has, and working out what that means.

Discovery is the authority on one thing only: what EXISTS. A listing is the
provider's own answer to "which models can this key call right now", and no
other source can know it. What those models ARE is the catalog's job, and the
two are merged per field with discovery winning — a provider reporting its own
model's context window is fact in a way a pattern match never is.

**Reconciliation is the half that did not exist before.** The old flow could
add models and remove them by hand, and that was all: a model the provider
retired stayed in the roster forever, failing every time it was tried and
getting benched on a cooldown timer, so the same dead end was rediscovered
every few hours for the life of the install. Comparing a fresh listing against
what is configured makes that a fact the system can hold — and holding it is
cheap, because a retired model is simply one that stopped being listed.

**Nothing here deletes a deployment.** A model vanishing from a listing is
usually retirement, but it is also what a lost API key, a changed plan or a
provider having a bad morning look like. Marking is reversible and silent
deletion is not, so a model that comes back is simply listed again and stops
being retired.

**Capability discovery is taken where it is genuinely offered.** Most
first-party APIs return an id and little else, but a gateway publishing which
parameters a model accepts is answering a question nothing else can: whether
reasoning can be asked for at all. That is read here into a real effort scheme
rather than left to be found out by a failed call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..catalog import Effort, EffortKind, EffortScheme, Lifecycle
from ..redact import redact
from . import deployments as deployment_store
from .connections import get_connection

logger = logging.getLogger(__name__)

#: Parameter names that mean "this model can be asked to reason". Anything a
#: provider publishes under its supported-parameters list; the spellings differ
#: between gateways, so several are recognised.
_REASONING_PARAMETERS = frozenset({
    "reasoning", "reasoning_effort", "include_reasoning", "thinking",
    "thinking_config", "thinking_budget", "thinking_level",
})

#: The ladder to assume when a provider says reasoning is accepted but not which
#: levels. Conservative on purpose: it stops at HIGH rather than claiming MAX,
#: because "this model takes a reasoning parameter" does not say how far it
#: goes, and a clamp costs nothing while an over-claim costs a failed call.
_DISCOVERED_TIERS = EffortScheme(
    kind=EffortKind.TIERS,
    levels=(Effort.MINIMAL, Effort.LOW, Effort.MEDIUM, Effort.HIGH),
    default=Effort.MEDIUM,
    native={Effort.MINIMAL: "minimal", Effort.LOW: "low",
            Effort.MEDIUM: "medium", Effort.HIGH: "high"},
)

#: Capability names a listing may report, mapped to the catalog's own.
_CAPABILITY_ALIASES = {
    "vision": "vision", "image": "vision", "images": "vision",
    "video": "video", "audio": "audio",
    "tools": "tools", "tool_use": "tools", "function_calling": "tools",
    "web_search": "web_search", "search": "web_search",
}


@dataclass(frozen=True)
class Listed:
    """One model a provider says it has, already in catalog terms."""

    model: str
    discovered: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Present:
    """A configured deployment the provider still lists, and what it said.

    Carries the listing row rather than just the id, because the facts in it are
    the point: a model already configured is exactly the one whose context
    window or parameter list we most want refreshed, and an earlier version of
    this dataclass held only ids — so `apply` had nothing to write and silently
    discarded everything the provider had just told us about every model the
    user actually uses.
    """

    deployment_id: str
    listed: Listed


@dataclass(frozen=True)
class Reconciliation:
    """What a fresh listing means for what is already configured."""

    connection_id: str
    #: Listed, and not configured here yet.
    added: tuple[Listed, ...] = ()
    #: Configured and still listed.
    present: tuple[Present, ...] = ()
    #: Configured and no longer listed.
    retired: tuple[str, ...] = ()
    #: Set when the provider could not be reached at all, which is NOT the same
    #: as it listing nothing — see `fetch`.
    error: str | None = None

    @property
    def reachable(self) -> bool:
        return self.error is None


def _capabilities_from(reported: Any) -> dict[str, bool]:
    """Read a listing's capability array, where one is offered.

    A local server increasingly reports these directly; naming varies, so the
    aliases above absorb it. Anything unrecognised is dropped rather than
    guessed at.
    """
    out: dict[str, bool] = {}
    if not isinstance(reported, Iterable) or isinstance(reported, (str, bytes)):
        return out
    for item in reported:
        name = _CAPABILITY_ALIASES.get(str(item).strip().lower())
        if name:
            out[name] = True
    return out


def effort_from_parameters(reported: Any) -> EffortScheme | None:
    """What a supported-parameters list says about reasoning.

    Three outcomes, and the third is the valuable one:

    * not reported at all -> None, meaning the catalog's answer stands
    * reported and includes a reasoning parameter -> a real scheme
    * reported and does NOT -> `EffortKind.NONE`, which is a DISCOVERED fact
      rather than an assumption, and the only way to learn it without spending a
      failed request to find out

    This is the documented way to use such a list — check before sending, rather
    than send and handle the rejection.
    """
    if not isinstance(reported, Iterable) or isinstance(reported, (str, bytes)):
        return None
    names = {str(item).strip().lower() for item in reported}
    if not names:
        return None
    if names & _REASONING_PARAMETERS:
        return _DISCOVERED_TIERS
    return EffortScheme(kind=EffortKind.NONE)


def normalise(raw: Iterable[dict[str, Any]] | None) -> list[Listed]:
    """Adapter listing rows into catalog-shaped facts.

    The adapters answer in their own wire's vocabulary and in camelCase; the
    catalog speaks one vocabulary in snake_case. Translating here rather than in
    each adapter keeps three wire formats from each having an opinion about what
    the catalog's fields are called.
    """
    out: list[Listed] = []
    for row in raw or []:
        if not isinstance(row, dict):
            continue
        model = str(row.get("model") or "").strip()
        if not model:
            continue

        facts: dict[str, Any] = {"lifecycle": Lifecycle.CURRENT.value}
        if row.get("label"):
            facts["label"] = row["label"]
        context = row.get("contextTokens", row.get("context_tokens"))
        if context:
            facts["context_tokens"] = int(context)

        capabilities = _capabilities_from(row.get("capabilities"))
        if capabilities:
            facts["capabilities"] = capabilities

        scheme = effort_from_parameters(
            row.get("supported_parameters", row.get("supportedParameters")))
        if scheme is not None:
            # Stored, so it has to be plain data — an EffortScheme object here is
            # a TypeError the moment a refresh writes it to the deployment.
            facts["effort"] = scheme.as_dict()

        out.append(Listed(model=model, discovered=facts))
    return out


def for_picker(rows: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """A provider's listing as the add-a-model picker shows it.

    Distinct from `normalise` above, which produces what gets STORED: this is
    camelCase, keeps the provider's own wording, and adds only what a person
    needs to choose between two hundred ids from a gateway — the lineage each
    one belongs to.

    Shared by the two places that produce a picker list (the discovery call and
    the custom-address probe) because they feed the same screen. A second copy
    would let the two disagree about how a model is grouped depending on which
    route the user came in by, which reads as a bug in the catalog rather than
    in the duplication that caused it.

    `family` and `provider` are `None` when no pattern matched. That is a real
    group the picker shows rather than hides — an unrecognised local model has
    to be pickable.
    """
    from ..catalog import resolve

    out: list[dict[str, Any]] = []
    for raw in rows or []:
        item = {"model": raw} if isinstance(raw, str) else dict(raw)
        if not item.get("model"):
            continue
        item.setdefault("label", item["model"])
        item.setdefault("contextTokens", None)
        # Never guessed from the name. The old build answered "free" for any id
        # containing "flash", including on a paid-tier key, and showed it as a
        # badge. `None` is the honest answer to a question a listing did not ask.
        item.setdefault("billing", None)
        version = resolve(model=str(item["model"]))
        item["family"] = version.family
        item["provider"] = version.provider if version.provider != "unknown" else None
        out.append(item)
    return out


def fetch(connection_id: str, *, secret: str | None = None) -> tuple[list[Listed], str | None]:
    """What this connection's provider says it has.

    Answers `(models, error)` rather than an empty list either way: "reached it,
    found nothing" and "could not reach it" are different problems with
    different fixes, and a caller that cannot tell them apart gives the wrong
    advice. Kept from the old discovery flow, which got this right.
    """
    from ..adapters import get_adapter

    conn = get_connection(connection_id)
    if conn is None:
        return [], "That connection no longer exists."

    entry: dict[str, Any] = {"adapter": conn.get("adapter"), "baseUrl": conn.get("baseUrl"),
                             "keyRequired": conn.get("keyRequired")}
    if conn.get("secretRef"):
        entry["secretRef"] = conn["secretRef"]
    if secret is not None:
        entry["secretValue"] = secret

    module = get_adapter(conn.get("adapter") or "openai-compatible")
    try:
        found = module.list_models(entry)
    except Exception as err:  # noqa: BLE001 — an unreachable address is an answer
        logger.info("discovery failed for %s: %s", connection_id, redact(str(err)))
        try:
            message = module.friendly_error(err)
        except Exception:  # noqa: BLE001
            message = str(err)
        return [], redact(message) or "Could not reach that address."

    return normalise(found), None


def reconcile(connection_id: str, listed: Iterable[Listed]) -> Reconciliation:
    """Compare a listing against what is configured on this connection.

    Pure apart from reading the deployment store: it decides nothing and writes
    nothing, so a caller can show the user what would change before anything
    does.
    """
    configured = [d for d in deployment_store.list_deployments()
                  if d.get("connectionId") == connection_id]
    by_model = {d.get("model"): d for d in configured}
    listed = list(listed)
    by_name = {row.model: row for row in listed}

    return Reconciliation(
        connection_id=connection_id,
        added=tuple(row for row in listed if row.model not in by_model),
        present=tuple(Present(deployment_id=str(d["id"]), listed=by_name[d["model"]])
                      for d in configured if d.get("model") in by_name),
        retired=tuple(str(d["id"]) for d in configured if d.get("model") not in by_name),
    )


def apply(reconciliation: Reconciliation) -> dict[str, int]:
    """Write what the listing established, and nothing else.

    For a model still listed, that means everything it reported — not only that
    it is still there. A model already configured is precisely the one whose
    context window and parameter list are worth refreshing, since it is the one
    being called.

    Adding the models in `added` is left to the caller: discovering that a
    gateway offers four hundred models is not consent to configure four hundred
    models.

    A deployment that is listed again stops being retired. The marking is a
    record of the last listing, not a verdict.
    """
    if not reconciliation.reachable:
        return {"retired": 0, "restored": 0, "refreshed": 0}

    restored = refreshed = 0
    for row in reconciliation.present:
        current = deployment_store.get_deployment(row.deployment_id)
        if current is None:
            continue
        if current["version"].lifecycle is Lifecycle.RETIRED:
            restored += 1
        facts = {**row.listed.discovered, "lifecycle": Lifecycle.CURRENT.value}
        if deployment_store.record_discovery(row.deployment_id, facts) is not None:
            refreshed += 1

    retired = 0
    for deployment_id in reconciliation.retired:
        if deployment_store.record_discovery(
                deployment_id, {"lifecycle": Lifecycle.RETIRED.value}) is not None:
            retired += 1

    return {"retired": retired, "restored": restored, "refreshed": refreshed}


def refresh(connection_id: str, *, secret: str | None = None) -> Reconciliation:
    """Fetch, reconcile and record, in one call.

    The whole point of discovery having a seam: a caller that just wants the
    roster brought up to date should not have to know the three steps, and a
    caller that wants to show a preview first can still use them separately.
    """
    listed, error = fetch(connection_id, secret=secret)
    if error is not None:
        return Reconciliation(connection_id=connection_id, error=error)
    result = reconcile(connection_id, listed)
    apply(result)
    return result
