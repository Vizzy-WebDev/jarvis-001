"""App Control's connector routes over HTTP — the catalogue, the OAuth start/
callback round trip, and the shared per-catalogue-entry client — driven
through a real `TestClient` against the same stub OAuth+MCP server
`test_connector_oauth.py` already proved `oauth.py` itself against.

What's worth pinning here is what a *route* adds on top of `oauth.py`: 404s
for an unknown catalogue entry/connector, the `clientSecret`-needs-`clientId`
guard, `/catalog/{id}/ensure` reusing an existing record rather than
duplicating it, a registered shared client being pre-seeded into a fresh
`ensure`, and `GET /api/sandbox/status` answering the shape the front end
expects.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from stub_oauth_server import StubOAuthServer


@pytest.fixture
def client(scratch):
    from jarvis.main import create_app

    return TestClient(create_app())


@pytest.fixture
def stub(scratch):
    server = StubOAuthServer()
    server.start()
    yield server
    server.stop()


def test_the_catalogue_lists_every_bundled_entry_with_no_connector_yet(client):
    body = client.get("/api/connectors/catalog").json()
    ids = [entry["id"] for entry in body["catalog"]]
    assert "notion" in ids and "github" in ids
    notion = next(e for e in body["catalog"] if e["id"] == "notion")
    assert notion["connectFlow"]["kind"] == "oauth_dcr"
    assert notion["connectorId"] is None
    assert notion["status"] is None


def test_ensure_creates_once_and_reuses_on_a_second_call(client):
    first = client.post("/api/connectors/catalog/notion/ensure").json()
    assert first["ok"] is True
    second = client.post("/api/connectors/catalog/notion/ensure").json()
    assert second["connectorId"] == first["connectorId"]

    listed = client.get("/api/connectors").json()["connectors"]
    match = [c for c in listed if c["id"] == first["connectorId"]]
    assert len(match) == 1


def test_ensure_on_an_unknown_catalog_entry_is_a_real_404(client):
    response = client.post("/api/connectors/catalog/not-a-real-service/ensure")
    assert response.status_code == 404


def test_a_registered_shared_client_is_pre_seeded_into_a_fresh_ensure(client):
    from jarvis.config import get_secret

    registered = client.post("/api/connectors/catalog/github/register-client",
                             json={"clientId": "shared-id", "clientSecret": "shared-secret"})
    assert registered.json() == {"ok": True}

    ensured = client.post("/api/connectors/catalog/github/ensure").json()
    connector = client.get(f"/api/connectors/{ensured['connectorId']}").json()["connector"]
    # `hasSecret` reflects config.secretRef (the OAuth TOKEN once connected),
    # never the shared client credential — that one is filed separately, under
    # its own ref, so it never leaks into what a general connector read shows.
    assert connector["config"]["hasSecret"] is False
    assert get_secret(f"connclient_{ensured['connectorId']}") == "shared-secret"

    cleared = client.delete("/api/connectors/catalog/github/register-client")
    assert cleared.json() == {"ok": True}


def test_register_client_without_a_client_id_is_refused(client):
    response = client.post("/api/connectors/catalog/notion/register-client", json={})
    assert response.status_code == 400


def test_redirect_uri_is_a_real_url_on_this_server(client):
    body = client.get("/api/connectors/oauth/redirect-uri").json()
    assert body["uri"].endswith("/api/connectors/oauth/callback")


def test_connect_then_callback_ends_with_a_real_working_connector(client, stub):
    created = client.post("/api/connectors", json={
        "type": "mcp", "label": "Stub", "config": {
            "connectFlow": {"kind": "oauth_dcr", "url": f"{stub.base_url}/mcp"}}}).json()
    connector_id = created["connector"]["id"]

    started = client.post(f"/api/connectors/{connector_id}/connect", json={}).json()
    assert started["ok"] is True
    assert "authUrl" in started

    import httpx

    page = httpx.get(started["authUrl"]).json()
    callback = client.get("/api/connectors/oauth/callback",
                          params={"code": page["code"], "state": page["state"], "iss": stub.base_url},
                          follow_redirects=False)
    assert callback.status_code == 200
    assert "Connected" in callback.text

    detail = client.get(f"/api/connectors/{connector_id}").json()
    assert detail["connector"]["status"]["state"] == "working"
    assert detail["connector"]["config"]["hasSecret"] is True

    disconnected = client.post(f"/api/connectors/{connector_id}/disconnect").json()
    assert disconnected["ok"] is True
    assert disconnected["connector"]["config"]["hasSecret"] is False
    # The record itself, and its tool permissions, survive a disconnect.
    assert client.get(f"/api/connectors/{connector_id}").status_code == 200


def test_connect_with_a_secret_but_no_client_id_is_refused(client, stub):
    created = client.post("/api/connectors", json={
        "type": "mcp", "label": "Stub", "config": {
            "connectFlow": {"kind": "oauth_dcr", "url": f"{stub.base_url}/mcp"}}}).json()
    response = client.post(f"/api/connectors/{created['connector']['id']}/connect",
                           json={"clientSecret": "orphaned"})
    assert response.status_code == 400


def test_connect_on_an_unknown_connector_is_a_real_404(client):
    assert client.post("/api/connectors/not-a-real-id/connect", json={}).status_code == 404


def test_an_unknown_callback_state_answers_ok_false_not_a_crash(client):
    response = client.get("/api/connectors/oauth/callback",
                          params={"code": "x", "state": "never-issued"})
    assert response.status_code == 200
    assert "Could not connect" in response.text


def test_sandbox_status_answers_the_shape_the_front_end_expects(client):
    body = client.get("/api/sandbox/status").json()
    assert body["backend"] in ("wsl", "restricted")
    assert body["isolation"] in ("strong", "weak")
    assert isinstance(body["setupSteps"], list)
