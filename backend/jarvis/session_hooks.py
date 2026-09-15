"""Per-session state that must be cleared when a conversation is reset.

Exists to stop a real omission. In the Node original, "start a new chat" clears
FOUR things, not one: the in-memory transcript, the session's sticky model pick,
its unlocked-tool set, and its sticky style request. Three of those live in
modules that land in Wave 2, so a Wave 1 port that simply called the transcript
reset would look complete, pass its tests, and quietly leak a pinned model and an
unlocked confirm-gated tool across the "new chat" boundary — the unlocked-tool
leak being a genuine safety property, not a tidiness one.

Rather than leave a comment asking someone to remember, each of those modules
registers its own cleanup here as it is ported, and the session layer calls
whatever is registered. A module that forgets to register is visible: this
registry's contents are asserted in the tests against the set of ported modules.

A zero-import leaf, deliberately — anything may import it without risking the
loader/runner circular-import invariant the root CLAUDE.md describes.
"""

from __future__ import annotations

import logging
from typing import Callable

logger = logging.getLogger(__name__)

_hooks: dict[str, Callable[[str], None]] = {}


def register_session_reset(name: str, fn: Callable[[str], None]) -> None:
    """Register per-session cleanup to run when `session_id` is reset.

    `name` identifies the owning module, so the registry can be inspected and
    asserted on rather than merely trusted.
    """
    _hooks[name] = fn


def registered_names() -> list[str]:
    return sorted(_hooks)


def run_session_resets(session_id: str) -> None:
    """Run every registered cleanup. One failing hook never stops the others —
    a half-reset session is worse than a noisy log."""
    for name, fn in list(_hooks.items()):
        try:
            fn(session_id)
        except Exception:
            logger.exception("session reset hook %r failed for %s", name, session_id)
