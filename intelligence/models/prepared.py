"""What passes between the layer and a driver — and nothing else does.

A driver receives a `Prepared` call (already routed and adapted: instructions
rendered, foreign provider state removed, schemas translated, canonical hints) and
a `ConnInfo` (where, with what key, with which quirks). It returns `DriverEvent`s.
It never sees a Request, a route or a policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping

from .types import Item, Tool


@dataclass(frozen=True)
class ConnInfo:
    name: str
    base_url: str
    api_key: str | None
    quirks: Mapping[str, Any] = field(default_factory=dict, hash=False)


@dataclass(frozen=True)
class RenderedSection:
    label: str
    text: str
    #: Part of the stable prefix a driver may mark for caching.
    stable: bool = False


OutputMode = Literal["text", "strict", "json_mode", "instructed"]


@dataclass(frozen=True)
class PreparedOutput:
    #: text: no constraint. strict: the server enforces the schema. json_mode: the
    #: server guarantees JSON, the schema is in the instructions. instructed: only the
    #: instructions ask for it. The last two are emulation, validated afterwards.
    mode: OutputMode = "text"
    schema: Mapping[str, Any] | None = None
    name: str = "output"


@dataclass(frozen=True)
class Prepared:
    endpoint_id: str
    model_id: str
    system: tuple[RenderedSection, ...]
    items: tuple[Item, ...]
    tools: tuple[Tool, ...] = ()
    output: PreparedOutput = PreparedOutput()
    #: Canonical level, already checked against the endpoint's reasoning_control.
    effort: str | None = None
    #: The stable prefix should be marked for caching (the endpoint caches).
    cache: bool = False
    parallel_tools: bool = True
    max_output_tokens: int | None = None
    #: default_params, then this driver's extensions, merged into the request body.
    params: Mapping[str, Any] = field(default_factory=dict, hash=False)
    caps: Mapping[str, Any] = field(default_factory=dict, hash=False)
    #: Makes a canonical tool-call id. Drivers call it for every tool call they
    #: report, and carry the provider's own id (if any) as a Sealed "ids" item.
    mint_id: Callable[[], str] = field(default=lambda: "call_0", compare=False, hash=False)


@dataclass(frozen=True)
class Discovered:
    """One model a server listed, with only what it reported about it."""

    model_id: str
    label: str | None = None
    capabilities: Mapping[str, Any] = field(default_factory=dict, hash=False)
    pricing: Any = None  # catalog.Pricing
    family: str | None = None
    upstream: str | None = None


class Unexpressible(ValueError):
    """A schema this endpoint's wire format cannot express without losing meaning."""


def deep_merge(base: Mapping[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in extra.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out
