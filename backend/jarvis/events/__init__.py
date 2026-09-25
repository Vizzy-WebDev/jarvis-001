"""The internal event bus (directive §38).

Subsystems publish what happened; the UI and cross-cutting observers subscribe.
Nothing pushes UI state directly, and nothing imports an observer to notify it.

The bus exists so that no caller invents its own payload shape (the front end matches on
a fixed event vocabulary) and so the turn loop does not depend on the subsystems that
merely record what a turn did (cost, self-model, improvement, ops-trace, personality),
which would make it untestable without them.
"""

from .bus import Event, EventBus, EventType, bus

__all__ = ["Event", "EventBus", "EventType", "bus"]
