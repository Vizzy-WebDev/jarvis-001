"""Models and connections, read only.

What matters here is what a picker depends on and what must never leave the
process: every model with whether it is ready, every connection with how many
models hang off it, which models are currently being skipped — and no secret,
ever.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from jarvis.gateway import availability, connections, registry


@pytest.fixture
def client(scratch):
    from jarvis.main import create_app

    availability.reset_for_tests()
    yield TestClient(create_app())
    availability.reset_for_tests()


def test_an_empty_install_answers_the_recorded_shape(client):
    assert client.get("/api/models").json() == {"connections": [], "models": [], "health": {}}


def test_a_connection_reports_how_many_models_hang_off_it(client):
    conn = connections.add_connection(adapter="openai-compatible", base_url="http://localhost:1234",
                                      label="local", provider="local", kind="local",
                                      key_required=False)
    registry.add_model(connection_id=conn["id"], model="one")
    registry.add_model(connection_id=conn["id"], model="two")

    body = client.get("/api/models").json()
    assert [c["modelCount"] for c in body["connections"]] == [2]
    assert len(body["models"]) == 2
    assert all("ready" in m for m in body["models"])


def test_a_secret_never_leaves_the_process(client):
    """`hasSecret` is what a screen needs — whether a key is set, never what."""
    conn = connections.add_connection(adapter="openai-compatible", base_url="https://api.example",
                                      label="cloud", provider="custom", kind="gateway",
                                      key_required=True, secret="sk-not-a-real-key-000")
    registry.add_model(connection_id=conn["id"], model="paid-model")

    raw = client.get("/api/models").text
    assert "sk-not-a-real-key-000" not in raw
    assert "secretRef" not in raw
    body = client.get("/api/models").json()
    assert body["connections"][0]["hasSecret"] is True
    assert body["models"][0]["hasSecret"] is True


def test_a_model_being_skipped_says_so_and_says_for_how_long(client):
    conn = connections.add_connection(adapter="openai-compatible", base_url="http://localhost:1234",
                                      label="local", provider="local", kind="local",
                                      key_required=False)
    model = registry.add_model(connection_id=conn["id"], model="flaky")
    availability.record(model["id"], "unreachable", detail="the connection was refused")

    health = client.get("/api/models").json()["health"]
    assert health[model["id"]]["kind"] == "unreachable"
    assert health[model["id"]]["reason"] == "the connection was refused"
    assert health[model["id"]]["retryInMs"] > 0
    # A model nobody has had trouble with is simply absent, not listed as fine.
    assert len(health) == 1


def test_the_provider_tiles_are_what_the_add_flow_offers(client):
    tiles = client.get("/api/models/providers").json()["providers"]
    assert [t["id"] for t in tiles] == ["openai", "anthropic", "gemini", "local", "custom"]
    # A tile names a PROVIDER, never a wire format: which adapter it resolves to
    # is server-side, and leaking it into the UI is the exact mistake the
    # provider catalogue exists to undo.
    assert not any(t["label"] == "openai-compatible" for t in tiles)
