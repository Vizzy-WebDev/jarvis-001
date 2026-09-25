"""The assistant state machine (§37).

The value of a machine over booleans is that impossible states cannot be
represented, so the tests are mostly about what it REFUSES.
"""

from __future__ import annotations

import pytest

from jarvis.assistant import AssistantState, IllegalTransition, State
from jarvis.assistant.state import TRANSITIONS
from jarvis.events import EventType
from jarvis.events.bus import EventBus


@pytest.fixture
def sm():
    return AssistantState(session_id="s1", event_bus=EventBus())


def test_starts_idle_and_not_busy(sm):
    assert sm.state is State.IDLE
    assert sm.is_busy is False


def test_a_normal_turn_walks_the_lifecycle(sm):
    sm.to(State.LISTENING)
    sm.to(State.TRANSCRIBING)
    sm.to(State.THINKING)
    sm.to(State.WAITING_FOR_TOOL)
    sm.to(State.THINKING)
    sm.to(State.SPEAKING)
    sm.to(State.IDLE)
    assert sm.state is State.IDLE


def test_illegal_transitions_raise(sm):
    """With booleans nothing stops isSpeaking and isTranscribing being true at
    once. Here the edge simply does not exist."""
    sm.to(State.LISTENING)
    sm.to(State.TRANSCRIBING)
    with pytest.raises(IllegalTransition):
        sm.to(State.SPEAKING)          # must pass through THINKING first
    assert sm.state is State.TRANSCRIBING   # and the state did not move


def test_is_busy_is_derived_not_stored(sm):
    assert sm.is_busy is False
    sm.to(State.THINKING)
    assert sm.is_busy is True
    sm.to(State.IDLE)
    assert sm.is_busy is False


def test_interrupt_works_from_every_active_state():
    for start in (State.THINKING, State.WAITING_FOR_TOOL, State.WAITING_FOR_APPROVAL,
                  State.EXECUTING, State.SPEAKING):
        sm = AssistantState(event_bus=EventBus())
        sm.to(State.THINKING)
        if start is not State.THINKING:
            sm.to(start)
        assert sm.interrupt() is True, f"could not interrupt from {start.value}"
        assert sm.state is State.INTERRUPTED


def test_interrupting_an_idle_assistant_is_a_no_op_not_an_error(sm):
    """Saying "stop" when nothing is happening is reasonable, not a fault."""
    assert sm.interrupt() is False
    assert sm.state is State.IDLE


def test_interruption_can_never_resume_speaking(sm):
    """§14: a barge-in must not corrupt conversation state. The assistant must
    not be able to carry on talking over someone who cut in — so there is no
    edge from INTERRUPTED to SPEAKING at all."""
    sm.to(State.THINKING)
    sm.to(State.SPEAKING)
    sm.interrupt()
    assert State.SPEAKING not in TRANSITIONS[State.INTERRUPTED]
    with pytest.raises(IllegalTransition):
        sm.to(State.SPEAKING)


def test_interruption_returns_to_capturing_the_user(sm):
    sm.to(State.THINKING)
    sm.to(State.SPEAKING)
    sm.interrupt()
    sm.to(State.LISTENING)          # the new request is captured
    assert sm.state is State.LISTENING


def test_speaking_can_return_to_listening_for_conversation_mode(sm):
    """§16: a follow-up must not require the wake word again."""
    sm.to(State.THINKING)
    sm.to(State.SPEAKING)
    sm.to(State.LISTENING)
    assert sm.state is State.LISTENING


def test_fail_is_always_reachable(sm):
    """A subsystem failing must never be blocked by the lifecycle, or the
    assistant gets stuck in the state it failed in."""
    for start in State:
        s = AssistantState(event_bus=EventBus())
        if start not in (State.IDLE,):
            # walk somewhere legal, then force-fail regardless
            s._state = start  # noqa: SLF001 — deliberately testing from every state
        s.fail("provider unreachable")
        assert s.state is State.ERROR
        assert s.reason == "provider unreachable"


def test_error_recovers_to_idle(sm):
    sm.fail("boom")
    sm.to(State.IDLE)
    assert sm.state is State.IDLE


def test_reset_works_from_anywhere(sm):
    sm.to(State.THINKING)
    sm.to(State.WAITING_FOR_APPROVAL)
    sm.reset()
    assert sm.state is State.IDLE


def test_every_transition_publishes_an_event():
    eb = EventBus()
    seen = []
    eb.subscribe(EventType.ASSISTANT_STATE, seen.append)
    sm = AssistantState(session_id="s9", event_bus=eb)

    sm.to(State.LISTENING)
    sm.to(State.TRANSCRIBING)

    assert [e.payload["to"] for e in seen] == ["listening", "transcribing"]
    assert seen[0].payload["from"] == "idle"
    assert seen[0].payload["sessionId"] == "s9"


def test_a_no_op_transition_publishes_nothing():
    """The UI does not need to redraw for a transition that did not happen."""
    eb = EventBus()
    seen = []
    eb.subscribe(EventType.ASSISTANT_STATE, seen.append)
    sm = AssistantState(event_bus=eb)
    sm.to(State.IDLE)
    assert seen == []


def test_all_ten_directive_states_exist():
    required = {
        "idle", "listening", "transcribing", "thinking", "waiting_for_tool",
        "waiting_for_approval", "executing", "speaking", "interrupted", "error",
    }
    assert {s.value for s in State} == required


def test_every_state_has_a_way_out():
    """A state with no outgoing edges is a trap the assistant can never leave."""
    for state, targets in TRANSITIONS.items():
        assert targets, f"{state.value} is a dead end"


def test_a_batch_of_tools_can_reach_the_gate_midway():
    """One step can ask for several tools and the second can be the one that
    needs a human — so EXECUTING must be able to park for approval."""
    sm = AssistantState("s", event_bus=EventBus())
    sm.to(State.THINKING)
    sm.to(State.EXECUTING)
    assert sm.can_go_to(State.WAITING_FOR_APPROVAL)
    sm.to(State.WAITING_FOR_APPROVAL)
    assert sm.state is State.WAITING_FOR_APPROVAL
