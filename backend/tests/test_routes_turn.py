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
from jarvis.main import create_app
from jarvis.policy import approvals as approval_store
from jarvis.session import get_active_session_id


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    conversation.reset_for_tests()
    assembly.reset_for_tests()
    bus.reset_for_tests()
    yield
    assembly.reset_for_tests()
    conversation.reset_for_tests()
    reset_db()


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


def test_a_fresh_install_with_no_models_says_so(client):
    assert client.get("/api/status").json() == {"configured": False}


# --- streaming a turn --------------------------------------------------------


def test_no_usable_model_is_reported_as_a_state_not_a_crash(client):
    events = events_from(client.get("/api/chat/stream", params={"message": "hello there"}))
    error = [e for e in events if e["type"] == "error"]
    assert error and error[0]["code"] == "no_model"
    # In plain words, and saying what to do about it — not a stack trace, and not a
    # sentence the person has to decode. (It used to say "no AI model"; with a real
    # model system it can say what is actually missing.)
    message = error[0]["error"].lower()
    assert "no model" in message and "model settings" in message


# --- the gate over HTTP ------------------------------------------------------

def _add_high_risk_capability(ran: list) -> None:
    assembly.get_registry().register(CapabilitySpec(
        id="test.delete_file", name="delete_file", description="delete a file",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        risk=Risk.HIGH, handler=lambda **kw: ran.append(kw) or "deleted"))


def test_an_unknown_approval_is_a_404_not_a_500(client):
    assert client.post("/api/approvals/apr_nope", json={"decision": "allow"}).status_code == 404


def test_a_meaningless_decision_is_refused(client):
    assert client.post("/api/approvals/apr_x", json={"decision": "maybe"}).status_code == 400


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


# --- observers (§38) ---------------------------------------------------------


# --- barge-in ----------------------------------------------------------------


def test_a_report_with_no_reply_to_mark_is_answered_not_a_crash(client):
    """A known, disclosed race. Its only cost is that the interruption went
    unrecorded — never wrong data — so it answers honestly rather than raising."""
    assert client.post("/api/chat/interrupt", json={"spokenText": "heard this"}).json() \
        == {"ok": False}
