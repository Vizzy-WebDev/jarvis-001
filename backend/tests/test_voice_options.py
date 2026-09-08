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
from jarvis.gateway import connections, registry


@pytest.fixture
def client(scratch):
    from jarvis.main import create_app

    return TestClient(create_app())


def _connect(adapter: str, *, key: str | None = "k") -> dict:
    conn = connections.add_connection(adapter=adapter, base_url="https://example.test",
                                      label=adapter, secret=key, provider="custom",
                                      kind="first-party", key_required=True)
    return registry.add_model(connection_id=conn["id"], model=f"{adapter}-model")


def test_with_nothing_configured_every_engine_says_why_not(client):
    engines = client.get("/api/voice/options").json()["engines"]
    assert [e["available"] for e in engines] == [False, False, False]
    assert all(e["reason"] for e in engines), \
        "an unavailable engine with no reason is the shape of thing people file bugs about"


def test_a_plain_model_unlocks_the_engines_that_only_need_a_model(client):
    _connect("openai-compatible")
    engines = {e["id"]: e for e in client.get("/api/voice/options").json()["engines"]}
    assert engines["pipeline"]["available"] is True
    assert engines["duplex"]["available"] is True
    # It declares no realtime API of its own, so that one stays unavailable.
    assert engines["realtime"]["available"] is False


def test_realtime_appears_because_an_adapter_DECLARES_it(client, monkeypatch):
    """Not because the code recognises a provider. Flipping the declaration on
    an adapter that does not have one is enough to make the engine appear —
    which is the whole point, and would fail against a hardcoded name."""
    from jarvis import adapters

    _connect("openai-compatible")
    assert voice.realtime_models() == []

    real = adapters.get_capabilities
    monkeypatch.setattr(adapters, "get_capabilities",
                        lambda name: {**real(name), "realtime": True})
    monkeypatch.setattr(voice, "get_capabilities", adapters.get_capabilities)

    engines = {e["id"]: e for e in client.get("/api/voice/options").json()["engines"]}
    assert engines["realtime"]["available"] is True
    assert engines["realtime"]["models"][0]["label"] == "openai-compatible-model"


def test_a_realtime_capable_model_that_is_not_ready_does_not_count(client):
    """Declaring the capability is not enough — it has to actually be usable."""
    conn = connections.add_connection(adapter="gemini", base_url=None, label="g",
                                      provider="gemini", kind="first-party",
                                      key_required=True)
    registry.add_model(connection_id=conn["id"], model="live-model")  # no key saved

    engines = {e["id"]: e for e in client.get("/api/voice/options").json()["engines"]}
    assert engines["realtime"]["available"] is False


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
