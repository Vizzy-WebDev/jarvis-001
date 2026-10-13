"""Connections, endpoints and aliases — and how the catalog of endpoints is built.

* A **connection** is a configured instance of a driver: two Ollama machines are
  two connections using one driver.
* An **endpoint** is one model on one connection, `connection/model-id`. It is
  the unit routing picks.
* An **alias** is a config name for an endpoint or a family. Callers use only
  aliases, never model or provider names.

The catalog is config ⊕ the last discovery ⊕ probe results, merged per
capability (`capabilities.merge`). Built from data; no network here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from . import capabilities as caps_mod
from .capabilities import CapValue
from .prepared import Discovered


@dataclass(frozen=True)
class Pricing:
    """US dollars per million tokens. Zero on both sides is a real price: free."""

    input: float
    output: float
    cached_input: float | None = None

    @property
    def free(self) -> bool:
        return self.input == 0 and self.output == 0

    def cost(self, *, input_tokens: int | None, output_tokens: int | None,
             cached_tokens: int | None = None) -> float | None:
        if input_tokens is None and output_tokens is None:
            return None
        cached = cached_tokens or 0
        fresh = max((input_tokens or 0) - cached, 0)
        cached_rate = self.cached_input if self.cached_input is not None else self.input
        return (fresh * self.input + cached * cached_rate + (output_tokens or 0) * self.output) / 1_000_000

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"input": self.input, "output": self.output}
        if self.cached_input is not None:
            out["cached_input"] = self.cached_input
        return out


@dataclass(frozen=True)
class ModelEntry:
    """What config says about one model on a connection. Every field optional."""

    capabilities: Mapping[str, Any] = field(default_factory=dict, hash=False)
    pricing: Pricing | None = None
    context: int | None = None
    family: str | None = None
    upstream: str | None = None
    label: str | None = None
    latency_ms: int | None = None
    #: Overrides the connection's trust for this model alone. Only config can say it.
    trust: str | None = None


@dataclass(frozen=True)
class Limits:
    concurrency: int | None = None
    rpm: int | None = None


@dataclass(frozen=True)
class Connection:
    name: str
    driver: str
    base_url: str
    trust: str
    secret_ref: str | None = None
    limits: Limits = Limits()
    quirks: str | None = None  # a quirk profile name
    default_params: Mapping[str, Any] = field(default_factory=dict, hash=False)
    discovery: bool = True
    models: Mapping[str, ModelEntry] = field(default_factory=dict, hash=False)
    #: For the settings screen only: its display name and the preset it came from.
    label: str | None = None
    preset: str | None = None
    #: Costs nothing per token. None = decided by trust (a local machine is free); an explicit
    #: value wins, so price and trust stay two separate facts.
    free: bool | None = None
    #: Trust for what a gateway serves from one upstream (lowercase upstream name -> trust).
    upstream_trust: Mapping[str, str] = field(default_factory=dict, hash=False)

    @property
    def is_free(self) -> bool:
        return self.free if self.free is not None else self.trust == "local"

    @property
    def ledger_name(self) -> str:
        """The provider name the cost ledger files this connection's usage and prices under."""
        return self.preset or self.driver


@dataclass(frozen=True)
class Alias:
    name: str
    endpoint: str | None = None
    family: str | None = None


@dataclass(frozen=True)
class Endpoint:
    id: str
    connection: str
    model_id: str
    capabilities: Mapping[str, CapValue] = field(default_factory=dict, hash=False)
    pricing: Pricing | None = None
    family: str | None = None
    upstream: str | None = None
    label: str | None = None
    latency_ms: int | None = None
    #: Named in config (kept whatever discovery says) — as opposed to only discovered.
    configured: bool = False
    #: Listed by the connection's last successful discovery.
    listed: bool = False
    #: Who may see data sent here: the model's own trust, else its upstream's, else the
    #: connection's. Only config sets any of them; a listing can never raise it.
    trust: str = "standard"
    #: Wire details from the model's profile, laid over the connection's quirks.
    wire: Mapping[str, Any] = field(default_factory=dict, hash=False)

    @property
    def kind(self) -> str | None:
        return caps_mod.kind_of(self.capabilities)


def endpoint_id(connection: str, model_id: str) -> str:
    return f"{connection}/{model_id}"


def split_endpoint_id(value: str) -> tuple[str, str]:
    """Connection names never contain '/', model ids often do."""
    connection, sep, model = value.partition("/")
    if not sep or not connection or not model:
        raise ValueError(f"“{value}” isn't an endpoint id (connection/model-id).")
    return connection, model


@dataclass(frozen=True)
class Catalog:
    connections: Mapping[str, Connection]
    endpoints: Mapping[str, Endpoint]
    aliases: Mapping[str, Alias]

    def resolve_alias(self, name: str) -> list[Endpoint]:
        """The endpoints an alias points at, in catalog order. Unknown → empty."""
        alias = self.aliases.get(name)
        if alias is None:
            return []
        if alias.endpoint:
            found = self.endpoints.get(alias.endpoint)
            return [found] if found else []
        return [e for e in self.endpoints.values() if alias.family and e.family == alias.family]


def build(connections: Mapping[str, Connection], aliases: Mapping[str, Alias], *,
          driver_defaults: Mapping[str, Mapping[str, Any]],
          quirk_caps: Mapping[str, Mapping[str, Any]],
          discovered: Mapping[str, list[Discovered]],
          probed: Mapping[str, Mapping[str, Any]],
          profile_for: Callable[[str, str], tuple[dict[str, Any], dict[str, Any], str | None]] | None = None,
          ) -> Catalog:
    """Each capability comes from the strongest source that has it: the driver's wire default
    and the connection's quirk profile (`declared`), then the model's profile, then
    `discovered[connection]` (its last successful listing, kept when a later one failed), then
    `probed[endpoint id]` (what a probe or a real refusal measured), then the person's own
    config for that model. `profile_for(driver, model_id)` answers (capabilities, wire, family)."""
    endpoints: dict[str, Endpoint] = {}
    for conn in connections.values():
        base = dict(driver_defaults.get(conn.driver) or {})
        base.update(quirk_caps.get(conn.quirks or "") or {})
        listing = {d.model_id: d for d in discovered.get(conn.name) or []}
        names = list(conn.models) + [m for m in listing if m not in conn.models]
        for model_id in names:
            entry = conn.models.get(model_id) or ModelEntry()
            found = listing.get(model_id)
            eid = endpoint_id(conn.name, model_id)
            profile_caps, wire, profile_family = (profile_for(conn.driver, model_id) if profile_for
                                                  else ({}, {}, None))
            configured = dict(entry.capabilities)
            if entry.context:
                configured["max_context_tokens"] = entry.context
            merged = caps_mod.merge(declared=base, profile=profile_caps,
                                    discovered=dict(found.capabilities) if found else None,
                                    probed=probed.get(eid), configured=configured, where=eid)
            upstream = entry.upstream or (found.upstream if found else None)
            trust = (entry.trust or (conn.upstream_trust.get(upstream.lower()) if upstream else None)
                     or conn.trust)
            endpoints[eid] = Endpoint(
                id=eid, connection=conn.name, model_id=model_id, capabilities=merged,
                pricing=entry.pricing or (found.pricing if found else None) or (
                    Pricing(0.0, 0.0) if conn.is_free else None),
                family=entry.family or (found.family if found else None) or profile_family,
                upstream=upstream,
                label=entry.label or (found.label if found else None),
                latency_ms=entry.latency_ms,
                configured=model_id in conn.models, listed=found is not None,
                trust=trust, wire=wire,
            )
    return Catalog(connections=dict(connections), endpoints=endpoints, aliases=dict(aliases))
