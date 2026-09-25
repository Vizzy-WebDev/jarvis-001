"""What a provider itself says is left — the PROVIDER-REPORTED number.

Kept apart from measured usage on purpose. A balance is the provider's own
accounting, arriving with its own units and its own lag; presenting it alongside
a count this build made itself, as though they were the same kind of fact, is
exactly the blending the owner asked never to happen.

**Deliberately not a heartbeat source.** The heartbeat exists to judge whether a
finding is worth interrupting for, and that judgment costs a model call. A
maintenance refresh produces no finding — there is nothing to judge — so it is a
plain periodic timer, off behind an interlock like every other timer here.

A reader is registered, not hardcoded into a dispatch: adding a provider is one
`register_reader()` call. No reader is registered at the moment — the one that
existed read the old model system's connections, which no longer exist.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable

from . import store

logger = logging.getLogger(__name__)

ENABLE_ENV = "JARVIS_COST_REFRESH"
REFRESH_INTERVAL_S = 6 * 60 * 60.0

#: provider ref -> a callable returning a detail dict, or None when this
#: provider is not configured on this machine.
Reader = Callable[[], dict[str, Any] | None]
_readers: dict[str, Reader] = {}
_timer: threading.Timer | None = None


def register_reader(provider_ref: str, reader: Reader) -> None:
    _readers[provider_ref] = reader


def registered_readers() -> list[str]:
    return sorted(_readers)


def refresh_all() -> dict[str, Any]:
    """Every registered reader, each isolated: one provider being down must not
    stop another's balance from being refreshed."""
    read: list[str] = []
    failed: dict[str, str] = {}
    for provider_ref, reader in list(_readers.items()):
        try:
            detail = reader()
        except Exception as err:  # noqa: BLE001 — a provider API failing is expected
            failed[provider_ref] = str(err)
            continue
        if detail is None:
            continue                     # not configured here; not a failure
        store.record_balance(provider_ref, detail)
        read.append(provider_ref)
    return {"read": read, "failed": failed}


# --- the timer ---------------------------------------------------------------

def is_enabled() -> bool:
    return os.environ.get(ENABLE_ENV) == "1"


def start() -> bool:
    """Periodic refresh. Does nothing unless JARVIS_COST_REFRESH is set — a real
    launch (main()) sets it; a test's own create_app() never does."""
    global _timer
    if not is_enabled() or _timer is not None:
        return False

    def run() -> None:
        global _timer
        try:
            refresh_all()
        except Exception:  # noqa: BLE001
            logger.exception("balance refresh failed")
        _timer = threading.Timer(REFRESH_INTERVAL_S, run)
        _timer.daemon = True
        _timer.start()

    run()
    return True


def stop() -> None:
    global _timer
    if _timer is not None:
        _timer.cancel()
        _timer = None
