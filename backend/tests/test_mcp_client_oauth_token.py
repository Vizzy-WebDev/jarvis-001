"""`mcp_client.py` resolving the right kind of credential for an MCP call.

The bug this guards: a connector `oauth.py` connected stores a whole TOKEN SET
under `secretRef` (access token, refresh token, expiry), never a bare bearer
string — reading it as a plain secret and sending the raw JSON straight through
as `Authorization: Bearer <json blob>` is broken on the very first real call.
`_resolve_token()` is the one seam that decides which path applies; this tests
it directly rather than standing up a full MCP protocol server, since the
credential-resolution bug lives entirely there; `test_connector_oauth.py`
already proves `oauth.get_access_token()` itself against a real server.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from jarvis.config import save_secret
from jarvis.connectors import mcp_client, store


@pytest.fixture(autouse=True)
def _isolate(scratch):
    yield


def test_an_oauth_connected_connector_resolves_through_get_access_token(scratch, monkeypatch):
    connector = store.add_connector(type="mcp", label="Stub", config={
        "connectFlow": {"kind": "oauth_dcr", "url": "https://example.invalid/mcp"},
        "secretRef": "connoauth_x"})
    save_secret("connoauth_x", json.dumps({
        "accessToken": "tok-real", "refreshToken": "r", "expiresAt": None,
        "clientId": "c", "tokenEndpoint": "https://example.invalid/token"}))

    async def run():
        return await mcp_client._resolve_token(connector["id"], connector["config"])

    assert asyncio.run(run()) == "tok-real"


def test_a_non_oauth_connector_still_reads_secret_ref_as_a_plain_token(scratch):
    connector = store.add_connector(type="mcp", label="Custom", config={
        "connectFlow": {"kind": "stdio", "command": "whatever"},
        "secretRef": "conn_plain"})
    save_secret("conn_plain", "plain-bearer-token")

    async def run():
        return await mcp_client._resolve_token(connector["id"], connector["config"])

    assert asyncio.run(run()) == "plain-bearer-token"


def test_a_connector_needing_no_auth_resolves_to_no_token(scratch):
    connector = store.add_connector(type="mcp", label="Open", config={
        "connectFlow": {"kind": "none", "url": "https://example.invalid/mcp"}})

    async def run():
        return await mcp_client._resolve_token(connector["id"], connector["config"])

    assert asyncio.run(run()) is None
