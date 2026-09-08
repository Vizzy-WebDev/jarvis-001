"""Defaults for a model: curated where we know it, name-guessed where we don't.

Everything here is a STARTING POINT the user can edit afterwards. Nothing in this
file is treated as fact once a model has been saved.

**Two fixes to the Node original, both real defects rather than preferences:**

1. `FAST_HINTS` used a bare `mini`, which matches inside "ge**mini**" — so every
   Gemini model, Pro included, scored as a fast/cheap one and outranked genuinely
   strong models on any latency-leaning balance. The hints now require word
   boundaries. `lite` had the same shape of bug ("e-lite" style names), and
   `pro\\b` was unanchored at the front.

2. The `known` table is matched on the model name exactly as before, but a
   guessed entry now records `guessed: True`, so nothing downstream mistakes a
   name-derived guess for a measured fact (§45).
"""

from __future__ import annotations

import re
from typing import Any

from ..adapters import get_capabilities

KNOWN: dict[str, dict[str, Any]] = {
    "gemini-3.5-flash": {"label": "Gemini 3.5 Flash",
                         "caps": {"tools": True, "streaming": True, "contextTokens": 1000000, "vision": True},
                         "tier": {"speed": 5, "quality": 3, "cost": 1}, "tags": ["fast", "cheap"]},
    "gemini-3.6-flash": {"label": "Gemini 3.6 Flash",
                         "caps": {"tools": True, "streaming": True, "contextTokens": 1000000, "vision": True},
                         "tier": {"speed": 5, "quality": 3, "cost": 1}, "tags": ["fast", "cheap"]},
    "gemini-3-pro": {"label": "Gemini 3 Pro",
                     "caps": {"tools": True, "streaming": True, "contextTokens": 1000000, "vision": True},
                     "tier": {"speed": 3, "quality": 5, "cost": 3}, "tags": ["reasoning"]},
    "claude-haiku-4-5": {"label": "Claude Haiku 4.5",
                         "caps": {"tools": True, "streaming": True, "contextTokens": 200000, "vision": True},
                         "tier": {"speed": 5, "quality": 3, "cost": 1}, "tags": ["fast", "cheap"]},
    "claude-sonnet-5": {"label": "Claude Sonnet 5",
                        "caps": {"tools": True, "streaming": True, "contextTokens": 200000, "vision": True},
                        "tier": {"speed": 3, "quality": 4, "cost": 2}, "tags": ["balanced"]},
    "claude-opus-5": {"label": "Claude Opus 5",
                      "caps": {"tools": True, "streaming": True, "contextTokens": 200000, "vision": True},
                      "tier": {"speed": 2, "quality": 5, "cost": 4}, "tags": ["reasoning", "writing"]},
    "gpt-5.6-luna": {"label": "GPT-5.6 Luna",
                     "caps": {"tools": True, "streaming": True, "contextTokens": 200000, "vision": True},
                     "tier": {"speed": 4, "quality": 4, "cost": 2}, "tags": ["balanced"]},
    "llama3.1": {"label": "Llama 3.1",
                 "caps": {"tools": True, "streaming": True, "contextTokens": 128000, "vision": False},
                 "tier": {"speed": 3, "quality": 3, "cost": 0}, "tags": ["local", "free"]},
    "mistral": {"label": "Mistral",
                "caps": {"tools": True, "streaming": True, "contextTokens": 32000, "vision": False},
                "tier": {"speed": 4, "quality": 3, "cost": 0}, "tags": ["local", "free"]},
}

# Word-bounded on purpose — see this module's docstring for what an unbounded
# `mini` did to every Gemini model's routing score.
FAST_HINTS = re.compile(r"\b(flash|haiku|mini|nano|lite|small|luna)\b", re.I)
QUALITY_HINTS = re.compile(r"\b(opus|ultra|pro|large|max|reasoning)\b", re.I)
VISION_HINTS = re.compile(r"\b(vision|vl|llava|multimodal|pixtral|moondream)\b", re.I)

_KEY_REQUIRED_HOSTS = re.compile(r"openai\.com|openrouter\.ai|groq\.com|together\.(ai|xyz)", re.I)
_AGGREGATOR_HOSTS = re.compile(r"openrouter\.ai|groq\.com|together\.(ai|xyz)", re.I)


def is_local_connection(adapter: str | None, base_url: str | None, kind: str | None) -> bool:
    """`kind`, when stored, decides outright — it was captured from a real probe
    rather than guessed from a URL. The regex is only the legacy fallback."""
    if kind:
        return kind == "local"
    return adapter == "openai-compatible" and bool(base_url) and not _KEY_REQUIRED_HOSTS.search(base_url)


def is_aggregator_connection(adapter: str | None, base_url: str | None, kind: str | None) -> bool:
    if kind:
        return kind == "gateway"
    return adapter == "openai-compatible" and bool(base_url) and bool(_AGGREGATOR_HOSTS.search(base_url))


def assumes_vision(adapter: str | None, model: str, base_url: str | None, kind: str | None) -> bool:
    """Optimistic for a first-party cloud host; name-hints only for a local
    server AND for a gateway, since both serve mostly text-only models under one
    connection. Measured cost of getting this wrong: 17 gateway models all
    claiming vision ranked a music model above Gemini for an image turn."""
    if VISION_HINTS.search(model or ""):
        return True
    return not is_local_connection(adapter, base_url, kind) and not is_aggregator_connection(adapter, base_url, kind)


def _guess(adapter: str | None, model: str, base_url: str | None, kind: str | None) -> dict[str, Any]:
    local = is_local_connection(adapter, base_url, kind)
    fast = bool(FAST_HINTS.search(model or ""))
    strong = bool(QUALITY_HINTS.search(model or ""))
    return {
        "label": model,
        "caps": {"tools": True, "streaming": True,
                 "contextTokens": 32000 if local else 128000,
                 "vision": assumes_vision(adapter, model, base_url, kind)},
        "tier": {"speed": 5 if fast else 2 if strong else 3,
                 "quality": 5 if strong else 2 if fast else 3,
                 "cost": 0 if local else 3 if strong else 1 if fast else 2},
        "tags": ["local"] if local else [],
        # Says out loud that these numbers came from the name, not from use.
        "guessed": True,
    }


def catalog_defaults(adapter: str | None, model: str, base_url: str | None = None,
                     kind: str | None = None) -> dict[str, Any]:
    known = KNOWN.get(model)
    return dict(known) if known else _guess(adapter, model, base_url, kind)


def with_capability_defaults(caps: dict[str, Any] | None, adapter: str | None, model: str,
                             base_url: str | None = None, kind: str | None = None) -> dict[str, Any]:
    """Fill in capability flags a saved model predates, at READ time.

    A value the user explicitly set always wins; only genuinely absent flags are
    filled. Filling from the adapter's ceiling is the right way round: guessing
    False would hide a capable model with no visible reason why.
    """
    ceiling = get_capabilities(adapter)
    out = dict(caps or {})
    for key in ("video", "audio", "vision", "webSearch"):
        if out.get(key) is None:
            out[key] = bool(ceiling.get(key))
    if not assumes_vision(adapter, model, base_url, kind):
        # Local and gateway-hosted models are where the name is the only signal,
        # and handing a video to a text-only local model wastes a long upload to
        # get a confused answer.
        if (caps or {}).get("video") is None:
            out["video"] = False
        if (caps or {}).get("vision") is None:
            out["vision"] = False
    return out


BILLING: dict[str, list[dict[str, Any]]] = {
    "gemini": [{"pattern": re.compile(r"flash", re.I), "billing": "free"},
               {"pattern": re.compile(r"pro", re.I), "billing": "paid"}],
    "anthropic": [{"pattern": re.compile(r"^claude-", re.I), "billing": "paid"}],
    "openai-compatible": [{"pattern": re.compile(r"^gpt-", re.I), "billing": "paid",
                           "requireOpenAIHost": True}],
}


def infer_billing(adapter: str | None, base_url: str | None, model: str | None,
                  kind: str | None = None) -> str:
    """Best-effort billing tier. 'local' beats everything — a self-hosted server
    is free by construction regardless of what its model is named."""
    if not model:
        return "unknown"
    if is_local_connection(adapter, base_url, kind):
        return "local"
    if model.endswith(":free"):
        return "free"
    for rule in BILLING.get(adapter or "", []):
        if rule.get("requireOpenAIHost") and base_url and not re.search(r"openai\.com", base_url, re.I):
            continue
        if rule["pattern"].search(model):
            return rule["billing"]
    return "unknown"
