"""Browsing the roster as provider -> family -> version.

The tree exists to say one thing the flat list could not: the same model
reached two ways is ONE version with two routes, not two unrelated rows. A
person looking at their own roster could not previously tell a duplicate from a
genuine second route with its own key, its own price and its own rate limit.

It is built from the deployments this install actually has, never from a
shipped model list — a browse route that enumerated models Jarvis "knows
about" would be a hardcoded roster wearing a hat, stale the week after it was
written.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from jarvis.gateway import connections, deployments


@pytest.fixture
def client(scratch):
    from jarvis.main import create_app

    return TestClient(create_app())


def _connection(client, label, **kw):
    defaults = dict(adapter="openai-compatible", base_url=f"https://{label}.example",
                    label=label, provider="custom", kind="gateway", key_required=True,
                    secret="sk-not-a-real-key-000")
    defaults.update(kw)
    return connections.add_connection(**defaults)


def _tree(client):
    return client.get("/api/models/catalog").json()["providers"]


def test_an_empty_roster_is_an_empty_tree_not_a_shipped_model_list(client):
    """The binding constraint. If anything appears here on a fresh install,
    this route has become a hardcoded catalog."""
    assert _tree(client) == []


def test_a_model_is_filed_under_its_maker_and_its_lineage(client):
    conn = _connection(client, "cloud")
    deployments.add_deployment(connection_id=conn["id"], model="claude-sonnet-5-20251001")

    [provider] = _tree(client)

    assert provider["id"] == "anthropic"
    [family] = provider["families"]
    assert family["id"] == "claude-sonnet"
    [version] = family["versions"]
    assert version["model"] == "claude-sonnet-5-20251001"


def test_the_same_model_reached_two_ways_is_one_version_with_two_routes(client):
    """What the flat list could not express, and the reason this tree exists."""
    direct = _connection(client, "direct")
    gateway = _connection(client, "gateway")
    deployments.add_deployment(connection_id=direct["id"], model="claude-opus-5")
    deployments.add_deployment(connection_id=gateway["id"], model="claude-opus-5")

    [provider] = _tree(client)
    [family] = provider["families"]
    [version] = family["versions"]

    assert len(version["deployments"]) == 2
    assert {d["connectionLabel"] for d in version["deployments"]} == {"direct", "gateway"}
    assert len({d["id"] for d in version["deployments"]}) == 2, "separate routes, separate ids"


def test_grouping_follows_the_maker_not_the_address_it_is_reached_at(client):
    """A gateway reselling somebody else's model is still serving that maker's
    model. Filing it under the plumbing would make one version look like two."""
    reseller = _connection(client, "reseller", provider="openrouter")
    deployments.add_deployment(connection_id=reseller["id"], model="anthropic/claude-haiku-4-5")

    [provider] = _tree(client)

    assert provider["id"] == "anthropic", "not 'openrouter', and not 'custom'"


def test_a_model_no_pattern_recognises_is_still_listed(client):
    """Unrecognised is a real group, not a reason to disappear. A local model
    belongs somewhere a person can find it."""
    conn = _connection(client, "local", kind="local", key_required=False, secret=None)
    deployments.add_deployment(connection_id=conn["id"], model="some-local-7b")

    [provider] = _tree(client)

    assert provider["id"] == "unknown"
    [family] = provider["families"]
    assert family["id"] == "some-local-7b", "its own group, keyed by the model id"


def test_the_unrecognised_group_sorts_last(client):
    """It is the least useful group to read first, and it is the one most
    likely to be large on a roster pointed at a gateway."""
    conn = _connection(client, "mixed")
    deployments.add_deployment(connection_id=conn["id"], model="mystery-model")
    deployments.add_deployment(connection_id=conn["id"], model="gemini-3-pro")

    assert [p["id"] for p in _tree(client)] == ["google", "unknown"]


def test_every_node_of_the_tree_carries_exactly_its_declared_keys(client):
    """All three levels, not just the interesting one. A declared shape that
    nothing checks is a comment that happens to be executable."""
    from jarvis.routes.models import (
        CATALOG_FAMILY_KEYS, CATALOG_PROVIDER_KEYS, CATALOG_VERSION_KEYS,
    )

    conn = _connection(client, "cloud")
    deployments.add_deployment(connection_id=conn["id"], model="claude-opus-5")

    [provider] = _tree(client)
    assert set(provider) == set(CATALOG_PROVIDER_KEYS)
    [family] = provider["families"]
    assert set(family) == set(CATALOG_FAMILY_KEYS)
    [version] = family["versions"]
    assert set(version) == set(CATALOG_VERSION_KEYS)


def test_a_version_node_carries_the_catalog_s_full_answer(client):
    """Including the three-state capabilities and the provenance — a screen
    offering to correct a fact needs to know which facts were matched from a
    name and which were observed."""
    from jarvis.routes.models import CATALOG_VERSION_KEYS

    conn = _connection(client, "cloud")
    deployments.add_deployment(connection_id=conn["id"], model="gemini-3-pro")

    version = _tree(client)[0]["families"][0]["versions"][0]

    assert set(version) == set(CATALOG_VERSION_KEYS)
    assert version["version"]["capabilities"]["vision"] == "unknown"
    assert version["version"]["provenance"]["family"] == "catalog"
    assert version["version"]["effort"]["kind"] == "tiers"


def test_a_route_reports_whether_it_can_actually_be_used(client):
    conn = _connection(client, "cloud")
    added = deployments.add_deployment(connection_id=conn["id"], model="gpt-5-mini")
    deployments.update_deployment(added["id"], {"enabled": False})

    [route] = _tree(client)[0]["families"][0]["versions"][0]["deployments"]

    assert route["enabled"] is False and route["ready"] is True


def test_no_secret_reaches_the_catalog_route(client):
    conn = _connection(client, "cloud")
    deployments.add_deployment(connection_id=conn["id"], model="gpt-5-mini")

    raw = client.get("/api/models/catalog").text
    assert "sk-not-a-real-key-000" not in raw and "secretRef" not in raw


def test_the_browse_route_is_not_shadowed_by_a_deployment_of_that_name(client):
    """`/api/models/<id>` shares a path space with the static segments beside
    it, so `catalog` is refused as an id — otherwise that one deployment would
    be impossible to edit or delete, with nothing reporting a problem."""
    conn = _connection(client, "cloud")
    added = deployments.add_deployment(connection_id=conn["id"], model="m", label="Catalog")

    assert added["id"] != "catalog"
    assert client.get("/api/models/catalog").status_code == 200
    assert client.patch(f"/api/models/{added['id']}", json={"label": "Renamed"}).status_code == 200
