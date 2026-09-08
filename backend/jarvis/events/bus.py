"""A small synchronous event bus with a named vocabulary.

Design constraints, each from a real requirement rather than a preference:

* **A named taxonomy, not free strings** (§38). An unknown event type is a
  programming error and raises, so a typo cannot silently produce an event
  nobody receives — the failure mode of the string-keyed broadcast this replaces.

* **One bad subscriber never stops the others** (§47, failure-first). A handler
  that raises is logged and skipped; publishing keeps going. An observer crashing
  must never take down the turn that emitted the event.

* **Publishing never blocks on a slow consumer.** UI consumers subscribe through
  a bounded queue rather than a callback, and a full queue drops its oldest event
  instead of stalling the publisher. A laggy browser tab must not slow the
  assistant down.

* **Thread-safe.** FastAPI runs sync endpoints in a threadpool and background
  workers have their own threads, so publish/subscribe are guarded.

Deliberately NOT built: persistence, replay, ordering guarantees across types,
or a broker. §58 says build the foundational interface correctly and not more.
Anything needing durability writes to its own store and publishes an event
saying so — the event is the notification, not the record.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger(__name__)


class EventType(str, Enum):
    """The vocabulary from §38, plus the few the audit showed are actually needed.

    Kept deliberately small. A new type is added when a real subscriber needs to
    distinguish it — not in anticipation.
    """

    # Assistant lifecycle
    ASSISTANT_INPUT = "assistant.input"
    ASSISTANT_STATE = "assistant.state"          # the state machine (§37)
    ASSISTANT_THINKING = "assistant.thinking"
    ASSISTANT_RESPONSE = "assistant.response"
    ASSISTANT_SPEAKING = "assistant.speaking"
    ASSISTANT_INTERRUPTED = "assistant.interrupted"

    # Capability execution
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"

    # Approval (§8) — a first-class flow, not a tool-result field
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RESOLVED = "approval.resolved"

    # Background work (§13/§32)
    JOB_CREATED = "job.created"
    JOB_UPDATED = "job.updated"
    JOB_COMPLETED = "job.completed"

    # Model gateway — what the cost and self-model observers subscribe to,
    # instead of being imported by the turn loop.
    MODEL_CALL_STARTED = "model.call_started"
    MODEL_CALL_COMPLETED = "model.call_completed"
    MODEL_CALL_FAILED = "model.call_failed"

    # Voice (§15/§16) — the UI needs to distinguish "the wake word fired" from
    # "the session closed on its own", because they mean different things to
    # the person in the room.
    VOICE_WAKE = "voice.wake"
    VOICE_MODE = "voice.mode"

    # The desktop (wave H). Two subscribers need to tell these apart: the badge
    # that says Jarvis can see the screen, and the control overlay, which is a
    # different thing entirely — one is observation, the other is control.
    SCREEN_WATCH = "screen.watch"
    CONTROL_SESSION = "control.session"

    # System
    SYSTEM_HEALTH_CHANGED = "system.health_changed"
    #: Something happened that the user should be told about. Subsystems
    #: publish this; the notification store is what listens.
    NOTIFICATION_CREATED = "notification.created"
    #: …and this is the stored row, announced to open tabs. Two types rather
    #: than one because a store that re-published what it consumed would feed
    #: itself, and because a browser wants the row (with its id, so it can be
    #: marked read), not the request that produced it.
    NOTIFICATION_STORED = "notification.stored"


@dataclass(frozen=True)
class Event:
    type: EventType
    payload: dict[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)


Handler = Callable[[Event], None]

# Bounded so a stalled consumer costs memory in a fixed amount, not without limit.
QUEUE_MAXSIZE = 512


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[EventType, list[Handler]] = {}
        self._wildcard: list[Handler] = []
        self._queues: list[queue.Queue[Event]] = []
        self._lock = threading.RLock()

    # --- subscribing ---------------------------------------------------------

    def subscribe(self, event_type: EventType | None, handler: Handler) -> Callable[[], None]:
        """Register a callback. `event_type=None` receives everything.

        Returns an unsubscribe function, so a caller never has to reach back into
        the bus's internals to detach.
        """
        with self._lock:
            if event_type is None:
                self._wildcard.append(handler)
            else:
                self._handlers.setdefault(event_type, []).append(handler)

        def unsubscribe() -> None:
            with self._lock:
                target = self._wildcard if event_type is None else self._handlers.get(event_type, [])
                if handler in target:
                    target.remove(handler)

        return unsubscribe

    def subscribe_queue(self) -> queue.Queue[Event]:
        """A bounded queue receiving every event — for SSE/WebSocket consumers.

        A queue rather than a callback specifically so a slow browser tab cannot
        block the assistant: when it fills, the OLDEST event is dropped and the
        newest kept, because a UI that has fallen behind wants current state, not
        a backlog of stale frames.
        """
        q: queue.Queue[Event] = queue.Queue(maxsize=QUEUE_MAXSIZE)
        with self._lock:
            self._queues.append(q)
        return q

    def unsubscribe_queue(self, q: queue.Queue[Event]) -> None:
        with self._lock:
            if q in self._queues:
                self._queues.remove(q)

    # --- publishing ----------------------------------------------------------

    def publish(self, event_type: EventType, payload: dict[str, Any] | None = None) -> Event:
        if not isinstance(event_type, EventType):
            # A typo must fail loudly here rather than producing an event with no
            # subscribers, which is how the string-keyed version hid mistakes.
            raise TypeError(f"Unknown event type: {event_type!r}")
        event = Event(type=event_type, payload=payload or {})

        with self._lock:
            handlers = list(self._handlers.get(event_type, ())) + list(self._wildcard)
            queues = list(self._queues)

        for handler in handlers:
            try:
                handler(event)
            except Exception:
                # Failure-first (§47): an observer that raises is isolated. The
                # turn that emitted this event must not fail because cost
                # tracking had a bad day.
                logger.exception("event subscriber failed for %s", event_type.value)

        for q in queues:
            try:
                q.put_nowait(event)
            except queue.Full:
                try:
                    q.get_nowait()      # drop the oldest
                    q.put_nowait(event)
                except queue.Empty:     # drained concurrently; nothing to do
                    pass
        return event

    # --- test support --------------------------------------------------------

    def reset_for_tests(self) -> None:
        with self._lock:
            self._handlers.clear()
            self._wildcard.clear()
            self._queues.clear()


bus = EventBus()
