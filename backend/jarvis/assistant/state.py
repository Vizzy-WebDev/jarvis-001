"""An explicit assistant state machine (directive §37).

§37 names the anti-pattern this replaces directly: "Avoid scattered boolean flags
such as isListening / isThinking / isSpeaking / isBusy / isExecuting when an
explicit state machine can represent the lifecycle more reliably." The front end
today has sixteen module-level mutable flags plus five per-engine ones, and the
only shared vocabulary is four strings the orb happens to understand.

Two properties make this a machine rather than a renamed string:

* **Illegal transitions raise.** SPEAKING cannot follow TRANSCRIBING without
  passing through THINKING. With booleans nothing stops two flags being true at
  once; here the impossible state cannot be represented. This is the whole point,
  so it is enforced rather than documented.

* **`is_busy` is derived, never stored.** The moment "busy" becomes its own flag
  it can disagree with the state, which is exactly how the current front end ends
  up with a mic button whose appearance and behaviour drift apart.

Interruption (§14, §17) is a first-class state, reachable from every state where
the assistant is actively doing something, and its only exits are back into
capturing the user — never straight into speaking. That encodes the requirement
that a barge-in "must not corrupt conversation state": you cannot resume talking
over the user by accident, because there is no edge for it.
"""

from __future__ import annotations

import threading
from enum import Enum

from ..events import EventType, bus as default_bus
from ..events.bus import EventBus


class State(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    WAITING_FOR_TOOL = "waiting_for_tool"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    EXECUTING = "executing"
    SPEAKING = "speaking"
    INTERRUPTED = "interrupted"
    ERROR = "error"


# The states in which the assistant is actively doing something on the user's
# behalf — the ones a barge-in can cut into.
_INTERRUPTIBLE = frozenset({
    State.THINKING,
    State.WAITING_FOR_TOOL,
    State.WAITING_FOR_APPROVAL,
    State.EXECUTING,
    State.SPEAKING,
})

# Anything that is not IDLE or ERROR means work is in flight.
_BUSY = frozenset({
    State.LISTENING,
    State.TRANSCRIBING,
    State.THINKING,
    State.WAITING_FOR_TOOL,
    State.WAITING_FOR_APPROVAL,
    State.EXECUTING,
    State.SPEAKING,
})

TRANSITIONS: dict[State, frozenset[State]] = {
    State.IDLE: frozenset({State.LISTENING, State.THINKING, State.ERROR}),
    State.LISTENING: frozenset({State.TRANSCRIBING, State.IDLE, State.ERROR}),
    State.TRANSCRIBING: frozenset({State.THINKING, State.IDLE, State.ERROR}),
    State.THINKING: frozenset({
        State.WAITING_FOR_TOOL, State.WAITING_FOR_APPROVAL, State.EXECUTING,
        State.SPEAKING, State.IDLE, State.INTERRUPTED, State.ERROR,
    }),
    State.WAITING_FOR_TOOL: frozenset({
        State.THINKING, State.EXECUTING, State.IDLE, State.INTERRUPTED, State.ERROR,
    }),
    State.WAITING_FOR_APPROVAL: frozenset({
        # Approved -> run it; denied/cancelled -> back to thinking or idle.
        State.EXECUTING, State.THINKING, State.IDLE, State.INTERRUPTED, State.ERROR,
    }),
    # EXECUTING -> WAITING_FOR_APPROVAL is real, not a shortcut: a step can ask
    # for several tools at once, and the second of them can be the one that needs
    # a human. The alternative — deciding policy for the whole batch before
    # executing any of it — would mean the orchestrator re-implementing the
    # permission check that the executor already owns.
    State.EXECUTING: frozenset({
        State.THINKING, State.WAITING_FOR_TOOL, State.WAITING_FOR_APPROVAL,
        State.SPEAKING, State.IDLE, State.INTERRUPTED, State.ERROR,
    }),
    # SPEAKING -> LISTENING is conversation mode (§16): a follow-up needs no wake word.
    State.SPEAKING: frozenset({State.IDLE, State.LISTENING, State.INTERRUPTED, State.ERROR}),
    # Interruption always returns to capturing the user. Deliberately NO edge to
    # SPEAKING: the assistant can never resume talking over someone who cut in.
    State.INTERRUPTED: frozenset({State.LISTENING, State.TRANSCRIBING, State.THINKING, State.IDLE}),
    State.ERROR: frozenset({State.IDLE, State.LISTENING}),
}


class IllegalTransition(RuntimeError):
    """Raised when code attempts a transition the lifecycle does not allow."""


class AssistantState:
    """One assistant's lifecycle. Instantiated per session."""

    def __init__(self, session_id: str = "main", event_bus: EventBus | None = None) -> None:
        self.session_id = session_id
        self._bus = event_bus if event_bus is not None else default_bus
        self._state = State.IDLE
        self._reason: str | None = None
        self._lock = threading.RLock()

    @property
    def state(self) -> State:
        return self._state

    @property
    def reason(self) -> str | None:
        return self._reason

    @property
    def is_busy(self) -> bool:
        """Derived, never stored — a stored copy is what lets 'busy' disagree
        with what the assistant is actually doing."""
        return self._state in _BUSY

    @property
    def can_interrupt(self) -> bool:
        return self._state in _INTERRUPTIBLE

    def can_go_to(self, target: State) -> bool:
        return target in TRANSITIONS[self._state]

    def to(self, target: State, reason: str | None = None) -> State:
        """Move to `target`, publishing the change. Raises on an illegal move."""
        with self._lock:
            current = self._state
            if target == current:
                # A no-op re-entry is not an error, but it is not an event either
                # — the UI does not need to redraw for a transition that did not
                # happen.
                return current
            if target not in TRANSITIONS[current]:
                raise IllegalTransition(
                    f"{current.value} -> {target.value} is not a legal transition "
                    f"(allowed: {sorted(s.value for s in TRANSITIONS[current])})"
                )
            self._state = target
            self._reason = reason

        self._bus.publish(
            EventType.ASSISTANT_STATE,
            {"sessionId": self.session_id, "from": current.value, "to": target.value, "reason": reason},
        )
        return target

    def interrupt(self, reason: str = "user interrupted") -> bool:
        """Barge-in. Returns False if there was nothing to interrupt.

        A no-op rather than an error when the assistant is idle: the user saying
        "stop" when nothing is happening is a reasonable thing to do, not a fault.
        """
        with self._lock:
            if self._state not in _INTERRUPTIBLE:
                return False
        self.to(State.INTERRUPTED, reason)
        return True

    def fail(self, reason: str) -> None:
        """Enter ERROR from wherever we are. Always legal — a subsystem failing
        must never be blocked by the lifecycle, or the assistant gets stuck in
        the state it failed in.

        Failing again while already in ERROR updates the reason and publishes,
        rather than being swallowed as a no-op: the CURRENT fault is what
        self-diagnosis (§33) and the user need to see, not the first one that
        happened to arrive. Only an identical repeat is suppressed.
        """
        with self._lock:
            current = self._state
            if current == State.ERROR and self._reason == reason:
                return
            self._state = State.ERROR
            self._reason = reason
        self._bus.publish(
            EventType.ASSISTANT_STATE,
            {"sessionId": self.session_id, "from": current.value, "to": State.ERROR.value, "reason": reason},
        )

    def reset(self) -> None:
        """Back to IDLE from anywhere — for a new conversation or recovery."""
        with self._lock:
            current = self._state
            if current == State.IDLE:
                return
            self._state = State.IDLE
            self._reason = None
        self._bus.publish(
            EventType.ASSISTANT_STATE,
            {"sessionId": self.session_id, "from": current.value, "to": State.IDLE.value, "reason": "reset"},
        )
