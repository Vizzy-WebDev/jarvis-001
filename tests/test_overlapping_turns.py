"""Two turns alive on one conversation: the older one must not touch the newer one's state.

Found live as "Something went wrong on my side: interrupted -> executing is not a legal
transition". The person stops (or talks over) a turn whose worker is still inside a tool and
sends something new. The new turn starts; then the old worker finishes its tool, notices it was
cancelled and moved the conversation's ONE state machine to INTERRUPTED — in the middle of the
new turn, whose next move (EXECUTING) the machine then refused.
"""

from __future__ import annotations

import threading

import pytest

from jarvis import conversation
from jarvis.assistant.state import AssistantState, IllegalTransition, State
from jarvis.capabilities import CapabilityRegistry, CapabilitySpec, Risk
from jarvis.db import reset_for_tests as reset_db
from jarvis.events import EventType
from jarvis.events.bus import EventBus
from jarvis.orchestrator import (
    Done, Failed, Interrupted, Orchestrator, StepComplete, TextChunk, ToolCall, ToolRan,
    TurnRequest,
)


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    conversation.reset_for_tests()
    yield
    conversation.reset_for_tests()
    reset_db()


def _tool(reg, name, handler):
    reg.register(CapabilitySpec(
        id=f"builtin.{name}", name=name, description=f"the {name} capability", risk=Risk.LOW,
        input_schema={"type": "object", "properties": {}}, handler=handler))


def test_a_superseded_turn_cannot_move_the_newer_turns_state():
    reg = CapabilityRegistry()
    ran: list[str] = []
    started, release = threading.Event(), threading.Event()

    def slow(**_):
        ran.append("slow")
        started.set()
        release.wait(10)
        return "slow done"

    _tool(reg, "slow_action", slow)
    _tool(reg, "quick_action", lambda **_: ran.append("quick") or "quick done")

    bus = EventBus()
    moves: list[tuple[str, str]] = []
    bus.subscribe(EventType.ASSISTANT_STATE, lambda e: moves.append((e.payload["from"], e.payload["to"])))

    first_cancel = threading.Event()
    first_events: list = []
    seen_during_second: dict = {}
    calls = {"n": 0}

    class Model:
        orch: Orchestrator

        def stream(self, *, messages, system, tools, session_id, need=None, model_id=None,
                   role=None):
            calls["n"] += 1
            if calls["n"] == 1:        # the first turn asks for the slow action
                yield StepComplete(tool_calls=(ToolCall("c1", "slow_action", {}),), model_id="stub")
            elif calls["n"] == 2:      # the second turn is thinking when the old worker wakes up
                release.set()
                worker.join(10)
                seen_during_second["alive"] = worker.is_alive()
                seen_during_second["state"] = self.orch.state_for("s1").state
                yield StepComplete(tool_calls=(ToolCall("c2", "quick_action", {}),), model_id="stub")
            elif calls["n"] == 3:
                yield TextChunk("all good")
                yield StepComplete(text="all good", model_id="stub")
            else:
                raise AssertionError("a stale turn asked the model for another step")

    model = Model()
    orch = model.orch = Orchestrator(model, registry=reg, event_bus=bus)

    worker = threading.Thread(target=lambda: first_events.extend(
        orch.run_turn(TurnRequest(text="do the slow thing", session_id="s1", turn_id="t1"),
                      first_cancel)))
    worker.start()
    assert started.wait(10)
    first_cancel.set()                  # the person stops it — its worker is still in the tool

    second = list(orch.run_turn(TurnRequest(text="do the quick thing", session_id="s1",
                                            turn_id="t2")))

    assert seen_during_second == {"alive": False, "state": State.THINKING}, \
        "the old turn moved the state while the new one was thinking"
    assert not any(isinstance(e, Failed) for e in second), second
    assert isinstance(second[-1], Done) and second[-1].text == "all good"
    assert [e.capability for e in second if isinstance(e, ToolRan)] == ["quick_action"]
    assert orch.state_for("s1").state is State.IDLE

    # The old turn did exactly what it had already started, and nothing more.
    assert ran == ["slow", "quick"]
    assert calls["n"] == 3, "the stale turn asked the model for another step"
    assert [e.capability for e in first_events if isinstance(e, ToolRan)] == ["slow_action"]
    assert not any(isinstance(e, (Done, Failed)) for e in first_events)
    assert moves, "the recorder saw no state changes, so it proves nothing"
    assert not any(to == "interrupted" for _, to in moves), moves
    # and it wrote no "interrupted" mark onto what is now the newer turn's transcript
    assert not any(m.get("interrupted") for m in conversation.get_messages("s1"))


def test_a_turn_that_is_still_the_newest_is_still_interrupted_normally():
    """The guard only silences SUPERSEDED turns: stopping the one that is current still ends in
    INTERRUPTED with what was heard recorded."""
    reg = CapabilityRegistry()
    cancel = threading.Event()

    class Talkative:
        def stream(self, **_):
            yield TextChunk("The first part ")
            cancel.set()
            yield TextChunk("and the rest nobody heard.")
            yield StepComplete(text="everything", model_id="stub")

    orch = Orchestrator(Talkative(), registry=reg, event_bus=EventBus())
    events = list(orch.run_turn(TurnRequest(text="tell me", session_id="s1"), cancel))

    assert [e.spoken_text for e in events if isinstance(e, Interrupted)] == ["The first part "]
    assert orch.state_for("s1").state is State.INTERRUPTED


def test_the_state_machine_itself_stays_strict():
    machine = AssistantState("s1", event_bus=EventBus())
    machine.to(State.THINKING)
    machine.interrupt()
    with pytest.raises(IllegalTransition):
        machine.to(State.EXECUTING)
