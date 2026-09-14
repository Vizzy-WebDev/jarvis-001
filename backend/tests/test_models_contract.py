"""The public shape of the models API, pinned so a change to it cannot be silent.

Nothing in this suite currently fails when the models routes change shape. They
answer by spreading the internal record verbatim (`{**entry}` minus the secret,
`routes/models.py`), and the TypeScript client closes every one of these
interfaces with `[key: string]: unknown` — so a renamed or dropped field
compiles cleanly on both sides and arrives in the browser as wrong rendering
rather than as a failure. The three recorded contract fixtures that touch this
area do not close the gap either: two of them were recorded against an empty
roster and pin only the envelope, so they stay green whether or not the row
shape moves.

This file is the missing failure. It asserts the exact SET of keys each models
route returns — not their values, which legitimately vary run to run — because
the set is what the front end is written against.

It is deliberately a characterisation test: it describes what the code does
today, not what it ought to do. Several of the keys it pins are ones the
rebuild will remove on purpose. When that happens this test fails, and editing
it is how the removal gets made deliberately and reviewably, rather than
discovered later in a browser.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from jarvis.gateway import availability, connections, registry

#: Every key `_public_model` puts on the wire today. `secretRef` is absent on
#: purpose and there is a separate test below that keeps it that way.
MODEL_KEYS = {
    "id", "label", "model", "connectionId", "enabled", "caps", "tier", "tags",
    "notes", "billing", "adapter", "baseUrl", "keyRequired", "kind", "provider",
    "connectionLabel", "hasSecret", "ready",
}

#: Every key `_public_connection` puts on the wire for a connection saved WITH a
#: provider recorded. A connection saved before the provider catalogue existed
#: gets `provider`/`kind` backfilled at read time instead, which is the same key
#: set by a different route through the code.
CONNECTION_KEYS = {
    "id", "label", "adapter", "baseUrl", "provider", "kind", "keyRequired",
    "createdAt", "hasSecret", "modelCount",
}

HEALTH_KEYS = {"reason", "kind", "retryInMs"}

TILE_KEYS = {
    "id", "label", "icon", "iconBg", "adapter", "baseUrl", "urlEditable",
    "keyRequired", "kind", "suggestions", "keyHint",
}

PREVIEW_KEYS = {"total", "notWorking", "byConnection"}
PREVIEW_CONNECTION_KEYS = {"id", "label", "count", "isFreeTier", "remaining"}

#: What the front end actually reads off a model, today, across every screen.
#: Measured from the source rather than assumed, and the gap is large: `caps`,
#: `tier`, `tags`, `notes`, `billing`, `adapter`, `baseUrl`, `keyRequired`,
#: `kind`, `provider`, `connectionLabel` and `hasSecret` are all served on every
#: model row and not one of them is looked at.
#:
#: `billing` is the trap worth naming — the front end does read `model.billing`,
#: but on a `DiscoveredModel` in the add-a-model flow, which is a different
#: shape from a different route. Grepping for the field name alone says this
#: list should contain it; following the type says it should not.
MODEL_KEYS_THE_UI_READS = {
    "id", "connectionId", "label", "model", "enabled", "ready",
}

CONNECTION_KEYS_THE_UI_READS = {
    "id", "label", "adapter", "baseUrl", "hasSecret", "modelCount",
}


@pytest.fixture
def client(scratch):
    from jarvis.main import create_app

    availability.reset_for_tests()
    yield TestClient(create_app())
    availability.reset_for_tests()


@pytest.fixture
def populated(client):
    """One connection with a key, one model under it, one recorded failure.

    A populated roster on purpose: an empty one exercises none of the row
    shapes, which is exactly why the recorded fixtures cannot catch a change
    to them.
    """
    conn = connections.add_connection(
        adapter="openai-compatible", base_url="https://api.example", label="cloud",
        provider="custom", kind="gateway", key_required=True, secret="sk-not-a-real-key-000")
    model = registry.add_model(connection_id=conn["id"], model="demo-model")
    availability.record(model["id"], "quota", detail="out of quota until later")
    return client


def test_a_model_row_carries_exactly_these_keys(populated):
    row = populated.get("/api/models").json()["models"][0]
    assert set(row) == MODEL_KEYS


def test_a_connection_row_carries_exactly_these_keys(populated):
    row = populated.get("/api/models").json()["connections"][0]
    assert set(row) == CONNECTION_KEYS


def test_the_models_envelope_carries_exactly_these_keys(populated):
    assert set(populated.get("/api/models").json()) == {"connections", "models", "health"}


def test_a_health_row_carries_exactly_these_keys(populated):
    health = populated.get("/api/models").json()["health"]
    assert set(next(iter(health.values()))) == HEALTH_KEYS


def test_a_provider_tile_carries_exactly_these_keys(client):
    for tile in client.get("/api/models/providers").json()["providers"]:
        assert set(tile) == TILE_KEYS


def test_the_recheck_preview_carries_exactly_these_keys(populated):
    body = populated.get("/api/models/recheck/preview").json()
    assert set(body) == PREVIEW_KEYS
    assert set(body["byConnection"][0]) == PREVIEW_CONNECTION_KEYS


def test_everything_the_ui_reads_is_actually_served(populated):
    """The half of the contract that matters most.

    A key set can change freely as long as nothing consumes what it dropped.
    This is the assertion that says which keys are load-bearing: if the rebuild
    removes one of these, a screen breaks.
    """
    body = populated.get("/api/models").json()
    assert MODEL_KEYS_THE_UI_READS <= set(body["models"][0])
    assert CONNECTION_KEYS_THE_UI_READS <= set(body["connections"][0])


def test_the_ui_reads_only_keys_that_are_actually_served(populated):
    """Guards the guard above: a consumed-keys list that drifted out of date
    would make the previous test pass while proving nothing."""
    body = populated.get("/api/models").json()
    assert MODEL_KEYS_THE_UI_READS <= MODEL_KEYS
    assert CONNECTION_KEYS_THE_UI_READS <= CONNECTION_KEYS
    assert set(body["models"][0]) == MODEL_KEYS


def test_no_secret_reaches_any_models_route(populated):
    """Pinned here as well as in test_routes_models.py, because this file is
    what the rebuild edits — and a shape change is exactly the moment a secret
    gets re-added to a response by accident."""
    for path in ("/api/models", "/api/models/providers", "/api/models/recheck/preview"):
        raw = populated.get(path).text
        assert "sk-not-a-real-key-000" not in raw
        assert "secretRef" not in raw
