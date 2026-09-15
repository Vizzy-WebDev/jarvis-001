"""One adapter per WIRE FORMAT, not per provider.

`openai_compatible` alone covers OpenAI, Ollama, LM Studio, OpenRouter, Groq
and anything else OpenAI-shaped, by pointing a provider's `base_url`
elsewhere — they didn't converge on this shape by coincidence, several of
them deliberately cloned OpenAI's own API so existing client code would work
against them unmodified. Forking that into one file per brand would mean the
same request-builder and the same streaming parser duplicated across files
that must then be kept in sync by hand — a real seam for silent drift, not an
organizational improvement. See `providers.py`'s own docstring for where the
real distinction (what KIND of thing is on the other end) actually lives —
it is not this one.

A provider earns its own adapter only when its actual capability surface has
genuinely diverged from the wire format it started from (a Responses-API-only
feature, a native structured-output mode the generic shape can't express) —
never for branding.
"""

from __future__ import annotations

from typing import Any

from . import anthropic as _anthropic
from . import gemini as _gemini
from . import openai_compatible as _openai_compatible

_REGISTRY: dict[str, Any] = {
    _anthropic.NAME: _anthropic,
    _gemini.NAME: _gemini,
    _openai_compatible.NAME: _openai_compatible,
}


def get_adapter(wire_format: str | None):
    module = _REGISTRY.get(wire_format or "")
    if module is None:
        raise KeyError(f"No adapter for wire format: {wire_format!r}")
    return module
