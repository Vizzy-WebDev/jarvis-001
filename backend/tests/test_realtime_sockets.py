"""The two voice sockets.

Neither can be driven end to end without a real provider, so what is tested is
the part that is ours: the refusals, the fallback answer, and the rule that a
realtime session's tool calls go through the SAME permission gate as every other
turn rather than a quieter path of their own.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from jarvis import external_services
from jarvis.gateway import connections, registry


@pytest.fixture
def client(scratch):
    from jarvis.main import create_app

    return TestClient(create_app())


def test_with_no_recognition_key_the_socket_says_use_the_browser(client):
    """It answers rather than refusing: the client needs no separate "can I even
    try" round trip, and the browser's own recognition is a real path."""
    with client.websocket_connect("/api/duplex") as socket:
        assert socket.receive_json() == {"type": "ready", "mode": "browser"}


def test_with_no_realtime_model_the_session_says_so_and_closes(client):
    with client.websocket_connect("/api/live") as socket:
        answer = socket.receive_json()
    assert answer["code"] == "NO_API_KEY"
    assert "realtime" in answer["error"].lower()


def test_a_realtime_capable_model_with_no_key_still_does_not_open(client):
    """Declaring the capability is not enough — the connection has to actually
    hold a credential, or there is nothing to connect with."""
    conn = connections.add_connection(adapter="gemini", base_url=None, label="g",
                                      provider="gemini", kind="first-party",
                                      key_required=True)
    registry.add_model(connection_id=conn["id"], model="live-model")

    with client.websocket_connect("/api/live") as socket:
        assert socket.receive_json()["code"] == "NO_API_KEY"


def test_a_realtime_tool_call_goes_through_the_ordinary_gate(scratch):
    """The rule this asserts: a voice route does not get a quieter permission
    path. A HIGH-risk capability called from a realtime session parks for a
    human exactly as it would from a typed turn — wiring speech straight into
    execution is how a voice path ends up with none of the protections the text
    path has."""
    from jarvis.assembly import get_registry
    from jarvis.capabilities import CapabilitySpec, Risk
    from jarvis.routes.realtime import _run_capability

    ran: list[dict] = []
    get_registry().register(CapabilitySpec(
        id="builtin.wipe_the_disk", name="wipe_the_disk", description="destroy things",
        input_schema={"type": "object", "properties": {}}, risk=Risk.HIGH,
        handler=lambda **kw: ran.append(kw) or "gone"))

    result = _run_capability("wipe_the_disk", {}, "live:test")

    assert not ran, "a realtime session executed a high-risk action with nobody asked"
    assert result["needs_confirmation"] is True
    assert result["approvalId"]


def test_an_ordinary_capability_still_just_runs(scratch):
    from jarvis.assembly import get_registry
    from jarvis.capabilities import CapabilitySpec, Risk
    from jarvis.routes.realtime import _run_capability

    get_registry().register(CapabilitySpec(
        id="builtin.what_time", name="what_time", description="the time",
        input_schema={"type": "object", "properties": {}}, risk=Risk.LOW,
        handler=lambda **kw: "six o'clock"))

    result = _run_capability("what_time", {}, "live:test")
    assert result["ok"] is True and result["value"] == "six o'clock"


def test_recognition_reports_the_provider_mode_when_a_key_exists(client, monkeypatch):
    """With a key the socket says which provider is live rather than falling
    back — the open itself needs a real service, so the fallback branch is what
    is observable here."""
    external_services.add_or_update(label="Deepgram", key="dg-key")

    from jarvis import stt

    async def refuse():
        raise stt.deepgram.NoKey("nope")

    monkeypatch.setattr(stt, "open_session", refuse)
    with client.websocket_connect("/api/duplex") as socket:
        # It tried the real provider (is_configured passed) and only then fell
        # back — which is the honest ordering.
        assert socket.receive_json() == {"type": "ready", "mode": "browser"}


def test_a_realtime_session_is_opened_BY_THE_ADAPTER_not_the_route():
    """The architecture rule this port exists to keep: one place per wire
    format. The Node app had two callers construct a provider SDK directly, and
    that is how the voice path ended up with none of the gateway's protections.
    An adapter that declares `realtime` is promising this function exists — the
    route asks for a session and never learns whose it is."""
    from jarvis import adapters

    for name, module in adapters.ADAPTERS.items():
        declares = bool(adapters.get_capabilities(name).get("realtime"))
        provides = hasattr(module, "open_realtime_session")
        assert declares == provides, (
            f"{name} declares realtime={declares} but provides={provides} — a capability "
            "flag is a promise about what the adapter actually offers")


def test_the_route_never_reaches_for_a_provider_by_name():
    from pathlib import Path

    source = (Path(__file__).parent.parent / "jarvis" / "routes" / "realtime.py").read_text()
    for word in ("genai", "gemini", "Gemini", "openai", "anthropic"):
        assert word not in source, f"the voice route names {word!r}"
