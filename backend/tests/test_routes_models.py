"""Models and connections, read only.

What matters here is what a picker depends on and what must never leave the
process: every model with whether it is ready, every connection with how many
models hang off it, which models are currently being skipped — and no secret,
ever.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from jarvis.model_system import health
from jarvis.model_system.providers import AuthMethod, ProviderKind, add_provider
from jarvis.model_system.registry import add_model
from jarvis.model_system.errors import ErrorKind


@pytest.fixture
def client(scratch):
    from jarvis.main import create_app

    yield TestClient(create_app())


def test_an_empty_install_answers_the_recorded_shape(client):
    assert client.get("/api/models").json() == {"connections": [], "models": [], "health": {}}


def test_a_connection_reports_how_many_models_hang_off_it(client):
    provider = add_provider(label="local", kind=ProviderKind.LOCAL, adapter="openai_compatible",
                            base_url="http://localhost:1234", auth_method=AuthMethod.NONE,
                            key_required=False)
    add_model(provider_id=provider.id, native_model_id="one")
    add_model(provider_id=provider.id, native_model_id="two")

    body = client.get("/api/models").json()
    assert [c["modelCount"] for c in body["connections"]] == [2]
    assert len(body["models"]) == 2
    assert all("ready" in m for m in body["models"])


def test_a_secret_never_leaves_the_process(client):
    """`hasSecret` is what a screen needs — whether a key is set, never what."""
    provider = add_provider(label="cloud", kind=ProviderKind.AGGREGATOR, adapter="openai_compatible",
                            base_url="https://api.example", auth_method=AuthMethod.API_KEY,
                            key_required=True, secret="sk-not-a-real-key-000")
    add_model(provider_id=provider.id, native_model_id="paid-model")

    raw = client.get("/api/models").text
    assert "sk-not-a-real-key-000" not in raw
    assert "credential_ref" not in raw
    assert "credentialRef" not in raw
    body = client.get("/api/models").json()
    assert body["connections"][0]["hasSecret"] is True
    assert body["models"][0]["hasSecret"] is True


def test_a_model_being_skipped_says_so_and_says_for_how_long(client):
    provider = add_provider(label="local", kind=ProviderKind.LOCAL, adapter="openai_compatible",
                            base_url="http://localhost:1234", auth_method=AuthMethod.NONE,
                            key_required=False)
    model = add_model(provider_id=provider.id, native_model_id="flaky")
    health.record_failure(model.id, ErrorKind.MODEL_UNAVAILABLE, detail="the connection was refused")

    body = client.get("/api/models").json()["health"]
    assert body[model.id]["kind"] == "unavailable"
    assert body[model.id]["reason"] == "the connection was refused"
    assert body[model.id]["retryInMs"] > 0
    # A model nobody has had trouble with is simply absent, not listed as fine.
    assert len(body) == 1


def test_the_provider_tiles_are_what_the_add_flow_offers(client):
    tiles = client.get("/api/models/providers").json()["providers"]
    assert [t["id"] for t in tiles] == ["openai", "anthropic", "gemini", "local", "custom"]
    # This exact shape (including the historical "openai-compatible" spelling
    # of `adapter`) is pinned by the recorded contract fixture
    # tests/contract/fixtures/0004-get-api-models-providers.json — it predates
    # the model-system rebuild and is not derived from its internal enums.
    assert tiles[0]["adapter"] == "openai-compatible"
    assert tiles[-1]["adapter"] is None
