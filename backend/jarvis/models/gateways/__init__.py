"""What a GATEWAY says about its models — read only for a connection that declares it.

A gateway (OpenRouter, OmniRoute) speaks the plain OpenAI-compatible chat format,
so its models are listed by `providers/openai_chat.py` like any other server's. What
it says BEYOND that — prices, modalities, which of its entries are its own routers —
is in fields of its own that no other server uses. Those are read here, one module
per gateway, and only when the connection says which gateway it is
(`Connection.gateway_kind`). The generic chat module never reads them, and nothing
is guessed from an address or an id: an undeclared connection is a plain server.

What comes out is only ever facts in the neutral shape (`types.Discovered`), merged
over whatever the chat module itself reported.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable
from urllib.parse import urlparse

from ..types import Discovered
from . import omniroute, openrouter

#: The gateways a connection can declare itself to be.
KINDS = ("openrouter", "omniroute")

_READERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "openrouter": openrouter.facts,
    "omniroute": omniroute.facts,
}

#: Ids a gateway is known to answer under for an id it was asked for, where neither the
#: namespace nor a dated snapshot explains it — `{gateway: {requested: {reported, ...}}}`.
#: Opt-in per gateway; empty until a real pairing is seen, never filled in by guesswork.
ALIASES: dict[str, dict[str, frozenset[str]]] = {"openrouter": {}, "omniroute": {}}


def annotate(gateway_kind: str | None, discovered: list[Discovered]) -> list[Discovered]:
    """The discovered models with what the declared gateway said about each one."""
    reader = _READERS.get(gateway_kind or "")
    if reader is None:
        return discovered
    out = []
    for item in discovered:
        extra = reader(item.raw) if isinstance(item.raw, dict) else {}
        facts = {**(item.facts or {}), **extra}
        out.append(replace(item, facts=facts or None))
    return out


def is_alias(gateway_kind: str | None, requested: str, reported: str) -> bool:
    return reported in ALIASES.get(gateway_kind or "", {}).get(requested, frozenset())


def default_for(base_url: str | None) -> str | None:
    """The gateway a NEW connection at this address is, when the address says so
    unambiguously — only OpenRouter's own host. The person can change it."""
    host = (urlparse(base_url or "").hostname or "").lower()
    return "openrouter" if host == "openrouter.ai" or host.endswith(".openrouter.ai") else None
