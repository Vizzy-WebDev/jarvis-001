"""What the voice picker is allowed to offer.

The rule under test: an engine or a voice appears because a capability check
says it is available, never because the code recognises a provider's name. The
tests below deliberately assert on CAPABILITIES rather than on brands, so an
implementation that special-cased one provider would fail them.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from jarvis import external_services
from jarvis.voice import options as voice


@pytest.fixture
def client(scratch):
    from jarvis.main import create_app

    return TestClient(create_app())


def test_with_nothing_configured_every_engine_says_why_not(client):
    engines = client.get("/api/voice/options").json()["engines"]
    assert [e["available"] for e in engines] == [False, False, False]
    assert all(e["reason"] for e in engines), \
        "an unavailable engine with no reason is the shape of thing people file bugs about"


def test_the_browsers_voice_is_always_there_and_always_first(client):
    """Free, offline, no key, no account — and it has no server side at all,
    which is exactly why it can never be missing."""
    voices = client.get("/api/voice/options").json()["voices"]
    assert voices[0]["id"] == "browser"
    assert voices[0]["configured"] is True and voices[0]["needsKey"] is False


def test_a_configured_voice_provider_joins_the_list(client):
    external_services.add_or_update(label="ElevenLabs", key="k")
    voices = client.get("/api/voice/options").json()["voices"]
    assert [v["id"] for v in voices] == ["browser", "elevenlabs"]
    assert voices[1]["needsKey"] is True


def test_listening_reports_which_mode_will_actually_run(client):
    assert client.get("/api/voice/options").json()["listening"] == {
        "mode": "browser", "serverProxied": False}

    external_services.add_or_update(label="Deepgram", key="dg")
    assert client.get("/api/voice/options").json()["listening"] == {
        "mode": "deepgram", "serverProxied": True}
