"""The HTTP surface of a turn: streaming, approvals, and the event stream.

Driven through FastAPI's real test client against a stub model server speaking
the genuine wire format, so what is exercised is the actual route, the actual
orchestrator, the actual adapter and the actual SSE framing.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from jarvis import assembly, conversation
from jarvis.capabilities import CapabilitySpec, Risk
from jarvis.db import reset_for_tests as reset_db
from jarvis.events import EventType, bus
from jarvis.gateway import availability, connections, registry
from jarvis.main import create_app
from jarvis.policy import approvals as approval_store

from stub_openai_server import StubModelServer


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    conversation.reset_for_tests()
    availability.reset_for_tests()
    assembly.reset_for_tests()
    bus.reset_for_tests()
    yield
    assembly.reset_for_tests()
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


@pytest.fixture
def client():
    return TestClient(create_app())


def events_from(response) -> list[dict]:
    out = []
    for line in response.text.splitlines():
        if line.startswith("data: "):
            out.append(json.loads(line[6:]))
    return out


# --- status ------------------------------------------------------------------

def test_status_is_configured_only_when_a_model_can_actually_be_used(client, stub):
    """A saved connection whose key was never entered is exactly the state this
    flow exists to catch."""
    assert client.get("/api/status").json()["configured"] is True

    keyed = connections.add_connection(adapter="anthropic", label="needs a key",
                                       provider="anthropic", kind="first-party",
                                       key_required=True)
    registry.add_model(connection_id=keyed["id"], model="claude-sonnet-5")
    # Still configured: one of the two is usable, which is the question asked.
    assert client.get("/api/status").json() == {"configured": True}


def test_a_fresh_install_with_no_models_says_so(client):
    assert client.get("/api/status").json() == {"configured": False}


# --- streaming a turn --------------------------------------------------------

def test_a_turn_streams_chunks_then_done(client, stub):
    stub.says("All quiet here.")
    response = client.get("/api/chat/stream", params={"message": "how's it going"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events = events_from(response)
    assert [e["type"] for e in events][0] == "routed"
    assert "".join(e["text"] for e in events if e["type"] == "chunk") == "All quiet here."
    assert events[-1] == {"type": "done", "text": "All quiet here.", "steps": 1}


def test_an_empty_message_is_refused_before_any_model_call(client, stub):
    assert client.get("/api/chat/stream", params={"message": "   "}).status_code == 400
    assert stub.requests == []


def test_no_usable_model_is_reported_as_a_state_not_a_crash(client):
    events = events_from(client.get("/api/chat/stream", params={"message": "hello there"}))
    error = [e for e in events if e["type"] == "error"]
    assert error and error[0]["code"] == "no_model"
    assert "no models set up" in error[0]["error"].lower()


def test_the_fast_path_answers_over_http_with_no_model_call(client, stub):
    """§10 end to end: the stub is scripted with nothing, so any model call is
    an error, and the answer still arrives."""
    response = client.get("/api/chat/stream", params={"message": "what's the time"})
    events = events_from(response)
    assert stub.requests == []
    assert events[0]["fast"] is True
    assert events[-1]["type"] == "done" and events[-1]["text"].startswith("It's ")


# --- the gate over HTTP ------------------------------------------------------

def _add_high_risk_capability(ran: list) -> None:
    assembly.get_registry().register(CapabilitySpec(
        id="test.delete_file", name="delete_file", description="delete a file",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        risk=Risk.HIGH, handler=lambda **kw: ran.append(kw) or "deleted"))


def test_a_high_risk_call_parks_and_only_a_separate_request_runs_it(client, stub):
    ran: list = []
    _add_high_risk_capability(ran)
    stub.calls_tool("delete_file", {"path": "notes.txt"})

    events = events_from(client.get("/api/chat/stream", params={"message": "delete my old notes"}))
    parked = [e for e in events if e["type"] == "approval_required"]
    assert parked and ran == []

    listed = client.get("/api/approvals").json()["approvals"]
    assert [a["id"] for a in listed] == [parked[0]["approvalId"]]
    assert listed[0]["risk"] == "high"

    stub.says("Done.")
    answer = client.post(f"/api/approvals/{parked[0]['approvalId']}", json={"decision": "allow"})
    assert answer.status_code == 200
    assert answer.json()["ran"] is True
    assert ran == [{"path": "notes.txt"}]


def test_denying_leaves_the_action_unrun(client, stub):
    ran: list = []
    _add_high_risk_capability(ran)
    stub.calls_tool("delete_file", {"path": "notes.txt"})
    events = events_from(client.get("/api/chat/stream", params={"message": "delete my old notes"}))
    approval_id = [e for e in events if e["type"] == "approval_required"][0]["approvalId"]

    body = client.post(f"/api/approvals/{approval_id}", json={"decision": "deny"}).json()
    assert body["ran"] is False and body["approval"]["status"] == "deny"
    assert ran == []


def test_voice_gets_the_same_gate_as_text(client, stub):
    """§8: 'A dangerous action should never bypass the gate simply because it
    originated from voice mode.'"""
    ran: list = []
    _add_high_risk_capability(ran)
    stub.calls_tool("delete_file", {"path": "notes.txt"})
    events = events_from(client.get("/api/chat/stream", params={
        "message": "delete my old notes", "source": "voice", "confidence": "0.95"}))
    assert [e for e in events if e["type"] == "approval_required"]
    assert ran == []


def test_an_unknown_approval_is_a_404_not_a_500(client):
    assert client.post("/api/approvals/apr_nope", json={"decision": "allow"}).status_code == 404


def test_a_meaningless_decision_is_refused(client):
    assert client.post("/api/approvals/apr_x", json={"decision": "maybe"}).status_code == 400


def test_a_redacted_argument_never_leaves_the_server(client, stub):
    assembly.get_registry().register(CapabilitySpec(
        id="test.send", name="send_message", description="send",
        input_schema={"type": "object", "properties": {"token": {"type": "string"}}},
        risk=Risk.HIGH, handler=lambda **_: "sent", redact_args=frozenset({"token"})))
    stub.calls_tool("send_message", {"token": "hunter2"})
    client.get("/api/chat/stream", params={"message": "send that message for me"})

    listed = client.get("/api/approvals").json()["approvals"]
    assert listed[0]["args"] == {"token": "<redacted>"}
    assert "hunter2" not in json.dumps(listed)


# --- the event stream --------------------------------------------------------
#
# Driven against a REAL server on a scratch port rather than the in-process test
# client: this stream only ends when the client disconnects, and an in-process
# client never actually closes a socket, so the test would hang on a stream that
# is behaving exactly as designed. A real socket is also the honest check —
# whether the browser receives what subsystems publish is a property of the
# transport, not of the generator.

def test_the_event_stream_delivers_what_subsystems_publish(live_server):
    import httpx

    with httpx.stream("GET", f"{live_server}/api/events", timeout=10) as response:
        assert response.status_code == 200
        lines = response.iter_lines()
        assert next(lines).startswith(": connected")
        bus.publish(EventType.NOTIFICATION_CREATED, {"title": "hello"})
        for line in lines:
            if line.startswith("data: "):
                payload = json.loads(line[6:])
                assert payload["type"] == "notification.created"
                assert payload["title"] == "hello"
                return
    raise AssertionError("the published event never arrived")


def test_a_second_tab_gets_its_own_stream(live_server):
    """Each subscriber has its own bounded queue, so one slow tab cannot starve
    another — or the publisher."""
    import httpx

    with httpx.stream("GET", f"{live_server}/api/events", timeout=10) as first, \
            httpx.stream("GET", f"{live_server}/api/events", timeout=10) as second:
        # One iterator per response: a second call to iter_lines() on the same
        # response is a StreamConsumed error, not a second read of the stream.
        streams = [first.iter_lines(), second.iter_lines()]
        for lines in streams:
            assert next(lines).startswith(": connected")
        bus.publish(EventType.NOTIFICATION_CREATED, {"title": "both"})
        for lines in streams:
            for line in lines:
                if line.startswith("data: "):
                    assert json.loads(line[6:])["title"] == "both"
                    break
            else:
                raise AssertionError("one of the two streams missed the event")
