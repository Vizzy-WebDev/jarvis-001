"""One real turn, top to bottom: orchestrator -> gateway -> adapter -> HTTP.

Nothing is mocked between the request and the wire. The only stand-in is the
server on the far end, which speaks the genuine OpenAI wire format — so this
exercises intent routing, context assembly, candidate selection, the real
adapter's SSE parsing, the permission gate, the executor and the transcript in
one pass, which no single-layer test can do.
"""

from __future__ import annotations

import pytest

from jarvis import conversation
from jarvis.capabilities import CapabilityRegistry, CapabilitySpec, Risk
from jarvis.db import reset_for_tests as reset_db
from jarvis.events.bus import EventBus
from jarvis.gateway import availability, connections, registry
from jarvis.gateway.client import Gateway
from jarvis.orchestrator import Chunk, Done, Orchestrator, ToolRan, TurnRequest

from stub_openai_server import StubModelServer


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    conversation.reset_for_tests()
    availability.reset_for_tests()
    yield
    availability.reset_for_tests()
    conversation.reset_for_tests()
    reset_db()


@pytest.fixture
def stub():
    server = StubModelServer()
    server.base_url = server.start()
    conn = connections.add_connection(adapter="openai-compatible", base_url=server.base_url,
                                      label="stub", provider="custom", kind="local",
                                      key_required=False)
    registry.add_model(connection_id=conn["id"], model="stub-model")
    yield server
    server.stop()


def run(stub, text, reg=None):
    orch = Orchestrator(Gateway(event_bus=EventBus()), registry=reg or CapabilityRegistry(),
                        event_bus=EventBus())
    return list(orch.run_turn(TurnRequest(text=text, session_id="s1")))


def test_a_plain_question_streams_back_a_real_answer(stub):
    stub.says("The kettle is on.")
    events = run(stub, "what are you up to")
    assert "".join(e.text for e in events if isinstance(e, Chunk)) == "The kettle is on."
    assert [e for e in events if isinstance(e, Done)][0].text == "The kettle is on."


def test_a_tool_call_round_trips_through_the_real_wire_format(stub):
    ran = []
    reg = CapabilityRegistry()
    reg.register(CapabilitySpec(
        id="builtin.get_weather", name="get_weather", description="weather",
        input_schema={"type": "object", "properties": {"where": {"type": "string"}}},
        risk=Risk.LOW, handler=lambda **kw: ran.append(kw) or "sunny"))

    stub.calls_tool("get_weather", {"where": "here"})
    stub.says("It's sunny here.")

    events = run(stub, "how's the weather looking today", reg=reg)

    assert ran == [{"where": "here"}]
    assert [e for e in events if isinstance(e, ToolRan)][0].ok
    assert [e for e in events if isinstance(e, Done)][0].text == "It's sunny here."

    # The model's second call really carried the tool result back over the wire.
    second = stub.requests[-1]["body"]["messages"]
    assert any(m.get("role") == "tool" and "sunny" in str(m.get("content")) for m in second)


def test_the_transcript_survives_the_round_trip(stub):
    stub.says("Noted.")
    run(stub, "remember I like tea")
    roles = [m["role"] for m in conversation.get_messages("s1")]
    assert roles == ["user", "assistant"]
