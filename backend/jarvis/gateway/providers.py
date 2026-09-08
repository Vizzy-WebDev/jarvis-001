"""The user-facing provider catalog — data only, zero imports.

A provider is what the user picks; an adapter is the wire format underneath it.
Keeping those two ideas apart is the whole reason this file exists: before the
split, an adapter name leaked into the UI as "OpenAI-compatible (OpenAI, Ollama,
LM Studio, OpenRouter, Groq, ...)" and a gateway had nowhere to type an address.

`kind` classifies WHAT is on the other end, independently of the wire format:
'first-party' | 'gateway' | 'local' | None (Custom, resolved by probing).
It is stored on a connection as a FACT captured at add time, never re-derived
from a host regex on every read — the change that made removing the per-gateway
tiles safe.
"""

from __future__ import annotations

import re
from typing import Any

PROVIDERS: list[dict[str, Any]] = [
    {
        "id": "openai", "label": "OpenAI", "icon": "🤖", "iconBg": "#10A37F",
        "adapter": "openai-compatible", "baseUrl": "https://api.openai.com/v1",
        "urlEditable": False, "keyRequired": True, "kind": "first-party",
        "suggestions": ["gpt-5.6-luna"],
        "keyHint": "Paste your OpenAI API key.",
    },
    {
        "id": "anthropic", "label": "Anthropic", "icon": "✳️", "iconBg": "#D97757",
        "adapter": "anthropic", "baseUrl": None,
        "urlEditable": False, "keyRequired": True, "kind": "first-party",
        "suggestions": ["claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5"],
        "keyHint": "Paste your Anthropic API key.",
    },
    {
        "id": "gemini", "label": "Gemini", "icon": "✨", "iconBg": "#4285F4",
        "adapter": "gemini", "baseUrl": None,
        "urlEditable": False, "keyRequired": True, "kind": "first-party",
        "suggestions": ["gemini-3.5-flash", "gemini-3.6-flash", "gemini-3-pro"],
        "keyHint": "Paste your Gemini API key.",
    },
    {
        "id": "local", "label": "Local server", "icon": "💻", "iconBg": "#4B5563",
        "adapter": "openai-compatible", "baseUrl": "http://localhost:11434/v1",
        "urlEditable": True, "keyRequired": False, "kind": "local",
        "suggestions": ["llama3.1", "mistral"],
        "keyHint": "Usually not needed for a local server.",
    },
    {
        "id": "custom", "label": "Custom", "icon": "🔧", "iconBg": "#6B7280",
        # Everything about a Custom row is resolved by probing the address.
        "adapter": None, "baseUrl": None,
        "urlEditable": True, "keyRequired": None, "kind": None,
        "suggestions": [],
        "keyHint": "Leave blank if the server needs no key — Jarvis will tell you if one is required.",
    },
]

_KNOWN_GATEWAY_HOSTS = re.compile(r"openrouter\.ai|groq\.com|together\.(ai|xyz)", re.I)


def get_provider(provider_id: str) -> dict[str, Any] | None:
    return next((p for p in PROVIDERS if p["id"] == provider_id), None)


def provider_for_legacy(adapter: str | None, base_url: str | None) -> dict[str, str | None]:
    """Read-time backfill for a connection saved before this catalog existed.

    Never migrates the file: old rows classify correctly when read, and the
    user's data is left exactly as they last saved it.
    """
    if adapter == "anthropic":
        return {"provider": "anthropic", "kind": "first-party"}
    if adapter == "gemini":
        return {"provider": "gemini", "kind": "first-party"}
    if adapter == "openai-compatible":
        if not base_url or re.search(r"openai\.com", base_url, re.I):
            return {"provider": "openai", "kind": "first-party"}
        if _KNOWN_GATEWAY_HOSTS.search(base_url):
            return {"provider": "custom", "kind": "gateway"}
        return {"provider": "custom", "kind": "local"}
    return {"provider": "custom", "kind": None}
