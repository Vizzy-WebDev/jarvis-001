"""The orchestrator (§9), driven end to end through a stub model.

The stub is a real `ModelClient` — the pipeline runs its actual production path,
including policy, the executor and the state machine. That is what makes the
claims here checkable rather than asserted: "the fast path spends no model call"
is a count on the stub, not a comment.

Weighted toward the properties the directive states as absolutes: voice never
bypasses the gate (§8), a high-risk action always parks for a human (§7), a
duplicate delivery never repeats a side effect (§49), an interruption never
corrupts the transcript (§14/§17), and a loop that gets nowhere says so instead
of running forever (§47).
"""

from __future__ import annotations

import threading

import pytest

from jarvis import conversation
from jarvis.assistant.state import State
from jarvis.capabilities import CapabilityRegistry, CapabilitySpec, Risk
from jarvis.capabilities.execute import ExecOutcome
from jarvis.db import reset_for_tests as reset_db
from jarvis.events import EventType
from jarvis.events.bus import EventBus
from jarvis.orchestrator import (
    ApprovalRequired, Chunk, Done, Failed, Interrupted, MAX_STEPS,
    Orchestrator, Routed, StepComplete, TextChunk, ToolCall, ToolRan, TurnRequest,
)
from jarvis.policy import Autonomy, Surface


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    conversation.reset_for_tests()
    yield
    conversation.reset_for_tests()
    reset_db()


class StubModel:
    """A scripted model. Each `stream()` call plays the next step."""

    def __init__(self, *steps):
        self.steps = [list(s) for s in steps]
        self.calls: list[dict] = []

    def stream(self, *, messages, system, tools, session_id, need=None):
        self.calls.append({"messages": list(messages), "system": system, "tools": tools})
        if not self.steps:
            raise AssertionError("the model was called more times than the test scripted")
        for event in self.steps.pop(0):
            yield event

    @property
    def call_count(self) -> int:
        return len(self.calls)


def say(text: str) -> list:
    return [TextChunk(text), StepComplete(text=text, model_id="stub")]


def call_tool(name: str, args: dict | None = None, call_id: str = "c1") -> list:
    return [StepComplete(tool_calls=(ToolCall(call_id, name, args or {}),), model_id="stub")]


@pytest.fixture
def reg():
    return CapabilityRegistry()


def add(reg, name, *, risk=Risk.LOW, handler=None, **kw):
    return reg.register(CapabilitySpec(
        id=f"builtin.{name}", name=name, description=f"the {name} capability",
        risk=risk, input_schema={"type": "object", "properties": {}},
        handler=handler or (lambda **_: "done"), **kw,
    ))


def run(orch, text, **kw) -> list:
    cancel = kw.pop("cancel", None)
    return list(orch.run_turn(TurnRequest(text=text, session_id="s1", **kw), cancel))


# --- the fast path (§10) -----------------------------------------------------

def test_a_fast_path_answer_costs_no_model_call_at_all(reg):
    add(reg, "get_time", handler=lambda **_: "It's 12:00.")
    model = StubModel()          # scripted with nothing: any call is an error
    orch = Orchestrator(model, registry=reg, event_bus=EventBus())

    events = run(orch, "what's the time")

    assert model.call_count == 0
    assert [type(e) for e in events] == [Routed, ToolRan, Done]
    assert events[0].fast is True
    assert events[-1].text == "It's 12:00."
    assert events[-1].steps == 0


def test_a_fast_capability_returning_data_does_not_get_a_sentence_invented(reg):
    """§45: turning data into prose is generation, so the model does it."""
    add(reg, "get_time", handler=lambda **_: {"hour": 12, "minute": 0})
    model = StubModel(say("It's noon."))
    orch = Orchestrator(model, registry=reg, event_bus=EventBus())

    events = run(orch, "what's the time")

    assert model.call_count == 1
    assert isinstance(events[-1], Done) and events[-1].text == "It's noon."
    # The result the model answered from is really in the transcript.
    roles = [m["role"] for m in conversation.get_messages("s1")]
    assert "tool" in roles


def test_a_fast_route_naming_a_capability_we_lack_falls_through(reg):
    """The router is static; the registry is populated at runtime. They may disagree."""
    model = StubModel(say("I can't open things yet."))
    orch = Orchestrator(model, registry=reg, event_bus=EventBus())

    events = run(orch, "open Chrome")

    assert events[0].fast is False
    assert model.call_count == 1


def test_nothing_said_is_answered_without_a_model_call(reg):
    model = StubModel()
    orch = Orchestrator(model, registry=reg, event_bus=EventBus())

    events = run(orch, "   ")

    assert model.call_count == 0
    assert isinstance(events[-1], Done)
    assert conversation.get_messages("s1") == []


# --- the model loop ----------------------------------------------------------

def test_a_plain_answer_is_one_step(reg):
    model = StubModel(say("Hello."))
    orch = Orchestrator(model, registry=reg, event_bus=EventBus())

    events = run(orch, "hello there")

    assert [type(e) for e in events] == [Routed, Chunk, Done]
    assert events[-1].steps == 1
    last = conversation.get_messages("s1")[-1]
    assert last["role"] == "assistant" and last["text"] == "Hello."


def test_a_tool_call_runs_and_the_model_answers_with_the_result(reg):
    ran = []
    add(reg, "get_weather", handler=lambda **_: ran.append(1) or "sunny")
    model = StubModel(call_tool("get_weather"), say("It's sunny."))
    orch = Orchestrator(model, registry=reg, event_bus=EventBus())

    events = run(orch, "how's the weather looking today")

    assert ran == [1]
    assert [type(e) for e in events] == [Routed, ToolRan, Chunk, Done]
    assert events[1].ok and events[1].outcome is ExecOutcome.COMPLETED
    assert events[-1].steps == 2
    # The second model call really saw the tool result.
    assert any(m["role"] == "tool" for m in model.calls[1]["messages"])


def test_the_model_is_never_told_a_capability_s_risk(reg):
    """§7: the permission decision must not be something the model can argue with."""
    add(reg, "delete_file", risk=Risk.HIGH)
    model = StubModel(say("ok"))
    orch = Orchestrator(model, registry=reg, event_bus=EventBus())

    run(orch, "hello there")

    declared = model.calls[0]["tools"]
    assert declared and set(declared[0]) == {"name", "description", "parameters"}


def test_a_turn_that_gets_nowhere_stops_and_says_so(reg):
    add(reg, "poke")
    model = StubModel(*[call_tool("poke", call_id=f"c{i}") for i in range(MAX_STEPS + 2)])
    orch = Orchestrator(model, registry=reg, event_bus=EventBus())

    events = run(orch, "keep poking at that thing")

    assert model.call_count == MAX_STEPS
    assert isinstance(events[-1], Failed)
    assert str(MAX_STEPS) in events[-1].error


def test_a_provider_failure_ends_the_turn_honestly(reg):
    class Broken:
        def stream(self, **_):
            raise RuntimeError("upstream is down")
            yield  # pragma: no cover — makes this a generator

    orch = Orchestrator(Broken(), registry=reg, event_bus=EventBus())
    events = run(orch, "hello there")

    assert isinstance(events[-1], Failed)
    assert orch.state_for("s1").state is State.ERROR


def test_a_client_that_never_completes_a_step_is_a_failure_not_a_silent_success(reg):
    model = StubModel([TextChunk("half a thought")])
    orch = Orchestrator(model, registry=reg, event_bus=EventBus())

    events = run(orch, "hello there")

    assert isinstance(events[-1], Failed)


# --- the gate (§7, §8) -------------------------------------------------------

def test_a_high_risk_tool_call_parks_for_a_human_and_ends_the_turn(reg):
    ran = []
    add(reg, "delete_file", risk=Risk.HIGH, handler=lambda **_: ran.append(1))
    model = StubModel(call_tool("delete_file"), say("done"))
    orch = Orchestrator(model, registry=reg, event_bus=EventBus())

    events = run(orch, "clear out the old logs for me")

    assert ran == []
    approvals = [e for e in events if isinstance(e, ApprovalRequired)]
    assert len(approvals) == 1 and approvals[0].capability == "delete_file"
    assert model.call_count == 1, "the turn must end at the gate, not loop on"
    assert orch.state_for("s1").state is State.WAITING_FOR_APPROVAL


def test_voice_does_not_bypass_the_gate(reg):
    """§8: 'A dangerous action should never bypass the gate simply because it
    originated from voice mode.' This is the defect verified in the Node app."""
    ran = []
    add(reg, "send_message", risk=Risk.HIGH, handler=lambda **_: ran.append(1))
    model = StubModel(call_tool("send_message"))
    orch = Orchestrator(model, registry=reg, event_bus=EventBus())

    events = run(orch, "text her that I'm on my way", surface=Surface.VOICE)

    assert ran == []
    assert any(isinstance(e, ApprovalRequired) for e in events)


def test_a_scheduled_task_still_needs_a_human_for_a_high_risk_action(reg):
    """The owner's explicit decision: pre-consent to a task is not consent to
    whatever it later decides to delete."""
    ran = []
    add(reg, "delete_file", risk=Risk.HIGH, handler=lambda **_: ran.append(1))
    model = StubModel(call_tool("delete_file"))
    orch = Orchestrator(model, registry=reg, event_bus=EventBus())

    events = run(orch, "run the nightly cleanup now",
                 surface=Surface.SCHEDULED, autonomy=Autonomy.PRE_CONSENTED)

    assert ran == []
    assert any(isinstance(e, ApprovalRequired) for e in events)


def test_an_allowlist_is_enforced_for_this_caller_too(reg):
    ran = []
    add(reg, "run_code", handler=lambda **_: ran.append(1))
    model = StubModel(call_tool("run_code"), say("I can't use that here."))
    orch = Orchestrator(model, registry=reg, event_bus=EventBus())

    events = run(orch, "run that script for me", allowed_names=frozenset({"get_time"}))

    assert ran == []
    refused = [e for e in events if isinstance(e, ToolRan)]
    assert refused and refused[0].outcome is ExecOutcome.REFUSED
    # And it was never offered in the first place.
    assert model.calls[0]["tools"] == []


# --- idempotency (§49) -------------------------------------------------------

def test_the_same_turn_delivered_twice_does_not_act_twice(reg):
    ran = []
    add(reg, "create_reminder", handler=lambda **_: ran.append(1) or "made it")
    orch = Orchestrator(
        StubModel(call_tool("create_reminder"), say("Done."),
                  call_tool("create_reminder"), say("Done.")),
        registry=reg, event_bus=EventBus(),
    )
    request = TurnRequest(text="set a reminder for nine", session_id="s1", turn_id="fixed")

    list(orch.run_turn(request))
    list(orch.run_turn(request))

    assert ran == [1], "the second delivery repeated a side effect"


# --- interruption (§14, §17) -------------------------------------------------

def test_an_interruption_records_what_was_actually_heard(reg):
    cancel = threading.Event()

    class Talkative:
        def stream(self, **_):
            yield TextChunk("The first part ")
            cancel.set()                    # the user cuts in here
            yield TextChunk("and the rest nobody heard.")
            yield StepComplete(text="everything", model_id="stub")

    orch = Orchestrator(Talkative(), registry=reg, event_bus=EventBus())
    events = list(orch.run_turn(
        TurnRequest(text="tell me about that", session_id="s1"), cancel))

    interrupted = [e for e in events if isinstance(e, Interrupted)]
    assert len(interrupted) == 1
    assert interrupted[0].spoken_text == "The first part "
    assert orch.state_for("s1").state is State.INTERRUPTED
    assert not any(isinstance(e, Done) for e in events)


# --- observability (§25, §38) ------------------------------------------------

def test_the_turn_reports_itself_on_the_event_bus(reg):
    add(reg, "get_weather")
    seen: list[str] = []
    ebus = EventBus()
    ebus.subscribe(None, lambda e: seen.append(e.type.value))
    orch = Orchestrator(
        StubModel(call_tool("get_weather"), say("sunny")), registry=reg, event_bus=ebus)

    run(orch, "how's the weather looking today")

    assert EventType.ASSISTANT_INPUT.value in seen
    assert EventType.MODEL_CALL_STARTED.value in seen
    assert EventType.MODEL_CALL_COMPLETED.value in seen
    assert EventType.TOOL_COMPLETED.value in seen
    assert EventType.ASSISTANT_RESPONSE.value in seen


def test_an_observer_that_raises_never_breaks_the_turn(reg):
    ebus = EventBus()
    ebus.subscribe(None, lambda e: (_ for _ in ()).throw(RuntimeError("bad observer")))
    orch = Orchestrator(StubModel(say("still fine")), registry=reg, event_bus=ebus)

    events = run(orch, "hello there")

    assert isinstance(events[-1], Done) and events[-1].text == "still fine"
