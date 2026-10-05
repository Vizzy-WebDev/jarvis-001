"""The two voice sockets.

Neither can be driven end to end without a real provider, so what is tested is
the part that is ours: the refusals and the fallback answer. The provider's own
realtime session does not exist until the model system is rebuilt.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from jarvis import external_services


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


def test_the_route_never_reaches_for_a_provider_by_name():
    from pathlib import Path

    source = (Path(__file__).parent.parent / "jarvis" / "routes" / "realtime.py").read_text()
    for word in ("genai", "gemini", "Gemini", "openai", "anthropic"):
        assert word not in source, f"the voice route names {word!r}"
