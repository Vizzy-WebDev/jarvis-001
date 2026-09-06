"""The internal event bus (directive §38).

Subsystems publish what happened; the UI and cross-cutting observers subscribe.
Nothing pushes UI state directly, and nothing imports an observer to notify it.

This replaces two patterns in the Node implementation:

  * `events.js` — a raw SSE broadcast with no event vocabulary at all, so every
    caller invented its own payload shape and the front end matched on strings.
  * `models/runner.js`'s six observer imports (cost, self-model x2, improvement,
    ops-trace, personality), which made the turn loop depend on five subsystems it
    has no business knowing about and could not be tested without them.
"""

from .bus import Event, EventBus, EventType, bus

__all__ = ["Event", "EventBus", "EventType", "bus"]
