"""Recording what capabilities did, from the events they already publish.

Every failure here is swallowed and logged: an observer that raises must never
take down the turn that emitted the event. That is the same isolation the bus
itself provides, restated at this end because a recorder that breaks the thing it
is recording is worse than no recorder at all.
"""

from __future__ import annotations

import logging
from typing import Callable

from ..events import EventType, bus as default_bus
from ..events.bus import Event, EventBus

logger = logging.getLogger(__name__)

_unsubscribes: list[Callable[[], None]] = []


def _record_tool_outcome(event: Event) -> None:
    from ..self import store as self_store

    capability = event.payload.get("capability")
    if not capability:
        return
    ok = event.type is EventType.TOOL_COMPLETED
    self_store.record_attempt("tool", capability, ok)


def start_observers(event_bus: EventBus | None = None) -> None:
    """Subscribe the recorders. Idempotent — calling twice does not double-count."""
    stop_observers()
    ebus = event_bus or default_bus
    for event_type in (EventType.TOOL_COMPLETED, EventType.TOOL_FAILED):
        _unsubscribes.append(ebus.subscribe(event_type, _record_tool_outcome))
    logger.info("[observers] recording capability outcomes")


def stop_observers() -> None:
    while _unsubscribes:
        try:
            _unsubscribes.pop()()
        except Exception:  # noqa: BLE001
            logger.warning("could not detach an observer")
