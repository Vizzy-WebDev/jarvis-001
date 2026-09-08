"""OAuth for MCP connectors, against a real PKCE-verifying authorization
server — no part of `jarvis.connectors.oauth` is mocked. What is worth testing
here is not "does an HTTP call happen" but that the security properties this
module exists for actually hold: PKCE really binds a code to whoever asked for
it, a callback's `iss` is really checked against RFC 9207, and a state is
really one-time.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from jarvis.config import get_secret, save_secret
from jarvis.connectors import oauth, store

from stub_oauth_server import StubOAuthServer


@pytest.fixture
def stub(scratch):
    server = StubOAuthServer()
    server.start()
    yield server
    server.stop()


@pytest.fixture
def connector(stub, scratch):
    return store.add_connector(type="mcp", label="Stub", config={
        "connectFlow": {"kind": "oauth_dcr", "url": f"{stub.base_url}/mcp"}})


async def _authorize(stub, connector_id: str, **manual) -> tuple[str, dict]:
    """Starts a connection and drives the returned authorize URL as a browser
    would, returning the redirect's own code+state."""
    started = await oauth.start_connect(connector_id, f"{stub.base_url}/mcp", **manual)
    assert "authUrl" in started, started
    async with httpx.AsyncClient() as client:
        page = (await client.get(started["authUrl"])).json()
    return started["authUrl"], page


def test_the_whole_round_trip_ends_with_a_real_working_token(stub, connector):
    async def run():
        _, page = await _authorize(stub, connector["id"])
        result = await oauth.handle_callback(
            {"code": page["code"], "state": page["state"], "iss": stub.base_url})
        assert result == {"ok": True, "connectorId": connector["id"]}
        assert stub.registered_clients, "Dynamic Client Registration never happened"

        token = await oauth.get_access_token(connector["id"])
        assert token == "tok-initial"

        saved = store.get_connector(connector["id"])
        assert saved["status"]["state"] == "working"
        assert saved["config"]["secretRef"]
        # The saved secret is the whole token SET, not a bare token — a bare
        # bearer string here would be sent to a completely different server as
        # `Authorization: Bearer <json blob>` the next time a call is made.
        blob = json.loads(get_secret(saved["config"]["secretRef"]))
        assert blob["accessToken"] == "tok-initial" and blob["refreshToken"] == "refresh-1"

    asyncio.run(run())


def test_an_expired_token_is_refreshed_transparently(stub, connector):
    async def run():
        _, page = await _authorize(stub, connector["id"])
        await oauth.handle_callback(
            {"code": page["code"], "state": page["state"], "iss": stub.base_url})

        ref = store.get_connector(connector["id"])["config"]["secretRef"]
        token_set = json.loads(get_secret(ref))
        # A real, past Unix timestamp — the honest way to force expiry, not a
        # sentinel value a real token would never actually hold.
        token_set["expiresAt"] = 0.0  # would be a real bug if this were treated as "no expiry"
        save_secret(ref, json.dumps(token_set))

        refreshed = await oauth.get_access_token(connector["id"])
        assert refreshed == "tok-refreshed"

    asyncio.run(run())


def test_a_server_that_needs_no_authorization_gets_no_browser_tab_at_all(stub, scratch):
    stub.require_auth = False
    connector = store.add_connector(type="mcp", label="Open", config={
        "connectFlow": {"kind": "oauth_dcr", "url": f"{stub.base_url}/mcp"}})

    async def run():
        return await oauth.start_connect(connector["id"], f"{stub.base_url}/mcp")

    result = asyncio.run(run())
    assert result == {"noAuthNeeded": True}
    saved = store.get_connector(connector["id"])
    assert saved["config"]["connectFlow"]["kind"] == "none"
    assert saved["status"]["state"] == "working"


def test_a_server_with_no_registration_endpoint_asks_for_a_client_id(stub, connector):
    stub.no_registration = True

    async def run():
        return await oauth.start_connect(connector["id"], f"{stub.base_url}/mcp")

    result = asyncio.run(run())
    assert result["needsManualClient"] is True
    assert result["reason"] == "unsupported"
    assert "Client ID" in result["message"]
    # Persisted, so the reason survives a reload rather than living only in
    # this one response.
    saved = store.get_connector(connector["id"])
    assert saved["config"]["connectFlow"]["manualClient"]["reason"] == "unsupported"

    # A past failure never forecloses trying again: a manually-supplied Client
    # ID on the very next attempt succeeds without ever touching /register.
    async def run_manual():
        return await oauth.start_connect(connector["id"], f"{stub.base_url}/mcp",
                                         manual_client_id="hand-typed-id")

    manual_result = asyncio.run(run_manual())
    assert "authUrl" in manual_result
    assert "hand-typed-id" in manual_result["authUrl"]
    assert not stub.registered_clients


def test_a_callback_missing_the_promised_iss_is_refused(stub, connector):
    """RFC 9207: once a server advertises that it always includes `iss`, a
    response missing it must be rejected outright, not tolerated as absent."""
    async def run():
        _, page = await _authorize(stub, connector["id"])
        return await oauth.handle_callback({"code": page["code"], "state": page["state"]})

    result = asyncio.run(run())
    assert result["ok"] is False
    assert "security information" in result["error"]


def test_a_mismatched_iss_is_refused_the_mix_up_attack_case(stub, connector):
    async def run():
        _, page = await _authorize(stub, connector["id"])
        return await oauth.handle_callback(
            {"code": page["code"], "state": page["state"], "iss": "http://attacker.example"})

    result = asyncio.run(run())
    assert result["ok"] is False
    assert "does not match" in result["error"]


def test_a_state_is_one_time_use_even_on_a_failed_attempt(stub, connector):
    """The rejection above still has to consume the state — otherwise a state
    parked in a browser's history could be replayed indefinitely."""
    async def run():
        _, page = await _authorize(stub, connector["id"])
        first = await oauth.handle_callback({"code": page["code"], "state": page["state"]})
        assert first["ok"] is False  # missing iss
        return await oauth.handle_callback(
            {"code": page["code"], "state": page["state"], "iss": stub.base_url})

    replay = asyncio.run(run())
    assert replay["ok"] is False
    assert "already used" in replay["error"]


def test_an_unknown_state_is_refused_not_a_crash(stub, scratch):
    async def run():
        return await oauth.handle_callback({"code": "x", "state": "never-issued"})

    result = asyncio.run(run())
    assert result == {"ok": False, "error": "This sign-in link is invalid or was already used.",
                      "connectorId": None}


def test_the_service_refusing_outright_is_reported_not_swallowed(stub, connector):
    async def run():
        return await oauth.handle_callback(
            {"error": "access_denied", "error_description": "the user said no", "state": "whatever"})

    result = asyncio.run(run())
    assert result["ok"] is False
    assert result["error"] == "the user said no"


def test_a_connector_needing_no_auth_returns_no_token_rather_than_raising(stub, scratch):
    connector = store.add_connector(type="mcp", label="Open", config={
        "connectFlow": {"kind": "none", "url": f"{stub.base_url}/mcp"}})

    async def run():
        return await oauth.get_access_token(connector["id"])

    assert asyncio.run(run()) is None


def test_disconnect_removes_the_token_but_keeps_the_connector(stub, connector):
    async def run():
        _, page = await _authorize(stub, connector["id"])
        await oauth.handle_callback(
            {"code": page["code"], "state": page["state"], "iss": stub.base_url})

    asyncio.run(run())
    ref = store.get_connector(connector["id"])["config"]["secretRef"]
    assert get_secret(ref)

    oauth.disconnect(connector["id"])
    assert get_secret(ref) is None
    assert store.get_connector(connector["id"]) is not None  # the record itself survives
