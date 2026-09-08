"""Every self-diagnosis check, and the one place the full list lives.

Adding a check is one import and one `register_check()` call here — never a
change to the registry or to the source that runs them.
"""

from __future__ import annotations

from ..registry import register_check
from . import capture_health, database, jobs, memory, scheduler, voice
from .security import config_integrity, event_spikes, listeners

_registered = False


def register_all() -> int:
    """Idempotent: called before the first heartbeat tick so the diagnosis source
    has real checks to run, and harmless if called again."""
    global _registered
    if _registered:
        return 0
    for module in (memory, database, jobs, scheduler, capture_health, voice,
                   config_integrity, event_spikes, listeners):
        register_check(module.CHECK)
    _registered = True
    return 9


def reset_for_tests() -> None:
    global _registered
    _registered = False
