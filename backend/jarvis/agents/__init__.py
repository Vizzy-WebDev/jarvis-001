"""Specialist agents — see `agents/CLAUDE.md`.

`ensure_builtins()` is the one seeding entry point. It is cheap (one read per
built-in), so every reader that needs the full roster calls it rather than
relying on some earlier caller having done it: a fresh data directory, a test's
scratch database and a real first launch all get the same agents.
"""

from __future__ import annotations

from . import store
from .builtins import BUILTIN_AGENTS


def ensure_builtins() -> list[str]:
    return store.seed_builtins(BUILTIN_AGENTS)
