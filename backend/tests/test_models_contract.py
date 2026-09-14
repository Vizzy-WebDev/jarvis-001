"""The public shape of the models API, pinned so a change to it cannot be silent.

Before this file, nothing in the suite failed when the models routes changed
shape. They answered by spreading the internal record verbatim (`{**entry}`
minus the secret), and the TypeScript client closes every one of these
interfaces with `[key: string]: unknown` — so a renamed or dropped field
compiled cleanly on both sides and arrived in the browser as wrong rendering
rather than as a failure. The three recorded contract fixtures that touch this
area do not close the gap either: two of them were recorded against an empty
roster and pin only the envelope, so they stay green whether or not the row
shape moves.

This file is the missing failure. It asserts the exact SET of keys each models
route returns — not their values, which legitimately vary run to run — because
the set is what the front end is written against.

**It has now done its job once.** It was written as a characterisation test
before the rebuild, describing what the code did rather than what it ought to,
specifically so that the keys the rebuild meant to remove would have to be
removed here by hand. At the switchover five of them were: `caps`, `tier`,
`tags`, `billing` and a `provider` that meant the connection's. Every one was
produced by matching regular expressions against the model's NAME and served to
the browser as fact. The list below was also what proved the removal safe —
`MODEL_KEYS_THE_UI_READS` was measured from the front-end source, and not one
of the five was in it.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from jarvis.gateway import availability, connections, deployments

#: Every key `_public_model` puts on the wire. `secretRef` is absent on purpose
#: and there is a separate test below that keeps it that way.
#:
#: `connectionProvider` is named for what it is. The row used to carry
#: `provider`, meaning the connection's, while the model's own maker had nowhere
#: to live at all — which is how the two axes quietly collapsed into one. The
#: maker is now `version.provider`, and the two can be told apart.
MODEL_KEYS = {
    "id", "label", "model", "connectionId", "enabled", "notes", "adapter",
    "baseUrl", "keyRequired", "kind", "connectionProvider", "connectionLabel",
    "version", "hasSecret", "ready",
}

#: What the catalog's answer looks like on the wire. Pinned for the same reason
#: as the row itself: this is what the rebuilt screens are written against.
VERSION_KEYS = {
    "provider", "model", "label", "family", "pinned", "contextTokens",
    "capabilities", "effort", "quality", "lifecycle", "provenance",
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

#: What the front end actually reads off a model, across every screen. Measured
#: from the source rather than assumed, and the gap was large: `caps`, `tier`,
#: `tags`, `notes`, `billing`, `adapter`, `baseUrl`, `keyRequired`, `kind`,
#: `provider`, `connectionLabel` and `hasSecret` were all served on every model
#: row and not one of them was looked at. That measurement is what made removing
#: five of them at the switchover a decision rather than a gamble.
#:
#: `billing` was the trap worth naming — the front end does read
#: `model.billing`, but on a `DiscoveredModel` in the add-a-model flow, which is
#: a different shape from a different route. Grepping for the field name alone
#: said this list should contain it; following the type said it should not.
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
    model = deployments.add_deployment(connection_id=conn["id"], model="demo-model")
    availability.record(model["id"], "quota", detail="out of quota until later")
    return client


def test_a_model_row_carries_exactly_these_keys(populated):
    row = populated.get("/api/models").json()["models"][0]
    assert set(row) == MODEL_KEYS


def test_the_version_block_carries_exactly_these_keys(populated):
    version = populated.get("/api/models").json()["models"][0]["version"]
    assert set(version) == VERSION_KEYS


def test_a_capability_nobody_has_established_is_served_as_unknown(populated):
    """Not as `false`. The old `caps` dict had two states, so a screen could
    only ever say "cannot see images" about a model nobody had asked — and the
    router read the same lie. All three states reach the browser."""
    capabilities = populated.get("/api/models").json()["models"][0]["version"]["capabilities"]
    assert capabilities["vision"] == "unknown"
    assert set(capabilities) == {"tools", "vision", "video", "audio", "web_search"}


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


def test_a_field_the_rebuild_removed_does_not_quietly_come_back(populated):
    """The removals, asserted rather than remembered.

    Each of these was a regex over the model's name served to the browser as a
    fact. The obvious way for one to return is somebody adding `caps` back
    because a screen wanted a boolean — which is the exact shape of the mistake,
    since the honest answer has three states and lives under `version`.
    """
    from jarvis.routes.models import REMOVED_MODEL_FIELDS

    row = populated.get("/api/models").json()["models"][0]
    assert set(REMOVED_MODEL_FIELDS) & set(row) == set()
    assert set(REMOVED_MODEL_FIELDS) & MODEL_KEYS == set()


def test_no_secret_reaches_any_models_route(populated):
    """Pinned here as well as in test_routes_models.py, because this file is
    what the rebuild edits — and a shape change is exactly the moment a secret
    gets re-added to a response by accident."""
    for path in ("/api/models", "/api/models/providers", "/api/models/recheck/preview"):
        raw = populated.get(path).text
        assert "sk-not-a-real-key-000" not in raw
        assert "secretRef" not in raw
