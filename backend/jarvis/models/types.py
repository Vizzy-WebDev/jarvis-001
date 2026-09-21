"""The plain shapes the store, the providers and the two callers share.

Deliberately NOT the orchestrator's port types. A provider yields these; only
`client.py` turns them into the port's events. That keeps every provider — and
the one-shot `ask` that tools reach through `jarvis.ai` — free of an import that
would pull the whole turn loop in behind it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Target:
    """Everything a provider needs to make one request: where, and with what key."""

    base_url: str
    api_key: str | None = None


@dataclass(frozen=True)
class Discovered:
    """One model a provider said it offers.

    `facts` is the little the PROVIDER reported that a request to this model
    needs, and nothing else: `{"maxOutput": int}` where a request must state an
    output ceiling and the ceiling differs per model, and
    `{"effort": {"levels": [...], "default": ...}}` where it reported which
    reasoning-effort levels the model accepts. It is never filled in from a table
    of what we believe a model can do — a key that is absent means the provider
    did not say, and nothing is offered or assumed on its behalf.
    """

    model_id: str
    label: str | None = None
    facts: dict[str, Any] | None = None


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    message: str


@dataclass(frozen=True)
class Usage:
    """What the provider itself reported. `None` is 'it did not say', never a zero.

    `tokens_in` is ALL the input the request carried, and `cached_in` the part of
    it that was served from a cache — so cached is a subset, whichever provider's
    own accounting had to be adjusted to say it that way.
    """

    tokens_in: int | None = None
    tokens_out: int | None = None
    tokens_reasoning: int | None = None
    cached_in: int | None = None


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolUse:
    id: str
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class Finished:
    """The end of one generation step. Exactly one closes every stream.

    `model_id` is what the provider SAID answered, which is not always what was
    asked for (an alias resolves to a dated snapshot; a router picks its own).
    `raw` is the provider's own reply, kept verbatim when its format needs it
    handed back exactly — `{"adapter": <format>, "content": ...}`.
    """

    text: str = ""
    tool_calls: tuple[ToolUse, ...] = ()
    finish_reason: str = "stop"  # 'stop' | 'tool_calls' | 'length' | 'content_filter'
    usage: Usage | None = None
    model_id: str | None = None
    raw: dict[str, Any] | None = None


ProviderEvent = TextDelta | Finished
