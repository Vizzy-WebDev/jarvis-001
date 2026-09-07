"""The composition root: where the parts are wired into one working assistant.

Everything else in this package is constructed with its collaborators passed in,
which is what makes each piece testable in isolation. Something still has to make
the real object graph, and doing that inside a route handler is how a second,
subtly different assembly gets built the next time a route needs one — the exact
shape of the Node app's three disagreeing model-call paths.

So it happens here, once, lazily, and `reset_for_tests()` tears it down.
"""

from __future__ import annotations

import logging
import threading

from .capabilities import CapabilityRegistry
from .events import bus
from .gateway.client import Gateway
from .orchestrator import Orchestrator
from .prefs import get_prefs
from .tools import load_tools

logger = logging.getLogger(__name__)

_lock = threading.RLock()
_registry: CapabilityRegistry | None = None
_orchestrator: Orchestrator | None = None


def get_registry() -> CapabilityRegistry:
    global _registry
    with _lock:
        if _registry is None:
            registry = CapabilityRegistry()
            names = load_tools(registry)
            # Logged at startup on purpose: whether every tool file actually
            # loaded is otherwise invisible until something tries to call one.
            logger.info("[assembly] %d capabilities available", len(names))
            _registry = registry
        return _registry


def get_orchestrator() -> Orchestrator:
    global _orchestrator
    with _lock:
        if _orchestrator is None:
            balance = str(get_prefs().get("balance") or "balanced")
            _orchestrator = Orchestrator(
                Gateway(balance=balance, event_bus=bus),
                registry=get_registry(),
                event_bus=bus,
            )
        return _orchestrator


def reset_for_tests() -> None:
    global _registry, _orchestrator
    with _lock:
        _registry = None
        _orchestrator = None
