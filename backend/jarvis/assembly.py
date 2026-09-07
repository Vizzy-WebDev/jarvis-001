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
from .observers import start_observers
from .gateway.client import Gateway
from .orchestrator import Orchestrator
from .prefs import get_prefs
from .tools import load_tools
from .voice import ConversationMode, WakeDetector

logger = logging.getLogger(__name__)

_lock = threading.RLock()
_registry: CapabilityRegistry | None = None
_orchestrator: Orchestrator | None = None
_wake: WakeDetector | None = None
_conversation_mode: ConversationMode | None = None


def get_registry() -> CapabilityRegistry:
    global _registry
    with _lock:
        if _registry is None:
            registry = CapabilityRegistry()
            names = load_tools(registry)
            # Subscribed here rather than called from the turn loop: what a
            # capability did is already published, and a recorder that has to be
            # invoked is a dependency the loop should not carry.
            start_observers(bus)
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


def get_wake_detector() -> WakeDetector:
    """One detector for the process. It holds a loaded model and an audio
    buffer, so a second one would score a different, half-length signal."""
    global _wake
    with _lock:
        if _wake is None:
            _wake = WakeDetector()
        return _wake


def get_conversation_mode() -> ConversationMode:
    global _conversation_mode
    with _lock:
        if _conversation_mode is None:
            _conversation_mode = ConversationMode()
        return _conversation_mode


def start_background_work() -> dict[str, bool]:
    """Switch on the things that run on their own clock.

    ONE place, so "what starts itself" is answerable by reading a single
    function. Every piece here is behind its own environment interlock and does
    nothing until cutover: the Node app is still the live one on the owner's
    machine, and two builds firing the same scheduled task, or polling the same
    provider account, act on shared state twice.
    """
    from .cost import balances, prices

    started = {"balances": False, "prices": False}
    if prices.is_refresh_enabled():
        seeded = prices.seed_local_model_prices()
        logger.info("[assembly] seeded %d local model prices", seeded)
        started["prices"] = True
    started["balances"] = balances.start()
    return started


def reset_for_tests() -> None:
    global _registry, _orchestrator, _wake, _conversation_mode
    with _lock:
        _registry = None
        _orchestrator = None
        _wake = None
        _conversation_mode = None
