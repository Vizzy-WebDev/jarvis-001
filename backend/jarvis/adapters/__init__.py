"""Three wire formats, three static imports (§58: no plugin framework).

`get_adapter(name)` is the only lookup. `get_capabilities(name)` returns what a
format can carry — used to SEED a model's own capability flags when it is added,
never to gate what a model is allowed to be asked (see base.py for why that
distinction matters and what it cost).
"""

from __future__ import annotations

from . import anthropic_adapter, gemini_adapter, openai_compatible

ADAPTERS = {
    openai_compatible.name: openai_compatible,
    anthropic_adapter.name: anthropic_adapter,
    gemini_adapter.name: gemini_adapter,
}

ADAPTER_NAMES = tuple(ADAPTERS)


def get_adapter(adapter_name: str | None):
    adapter = ADAPTERS.get(adapter_name or "")
    if adapter is None:
        raise KeyError(f"No such adapter: {adapter_name!r}")
    return adapter


def get_capabilities(adapter_name: str | None) -> dict[str, bool]:
    """An adapter that declares nothing is treated as text-only."""
    adapter = ADAPTERS.get(adapter_name or "")
    if adapter is None:
        return {"video": False, "audio": False, "vision": False, "webSearch": False}
    return dict(getattr(adapter, "CAPABILITIES", {}))
