"""Connector reliability: what makes "connected" mean connected.

Each test pins one confirmed failure from the connector investigation (Oct 2026).
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import httpx
import pytest

from jarvis.config import get_secret, save_secret
from jarvis.connectors import capabilities, mcp_client, oauth, store
from jarvis.db import reset_for_tests as reset_db

sys.path.insert(0, str(Path(__file__).parent))
from stub_mcp_server import StubMcpHttp  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    yield
    reset_db()


# --- discovery: an issuer with a path (GitHub: https://github.com/login/oauth) ------------------

def test_an_authorization_server_whose_issuer_has_a_path_is_found():
    """RFC 8414 puts `/.well-known/oauth-authorization-server` BEFORE the issuer's
    path. Jarvis asked the library for that with the issuer in the wrong argument, so
    it only ever tried the bare origin — and GitHub's real metadata, which lives at
    `github.com/.well-known/oauth-authorization-server/login/oauth`, was never found."""
    issuer = "https://auth.example.test/login/oauth"
    document = {"issuer": issuer, "authorization_endpoint": f"{issuer}/authorize",
                "token_endpoint": f"{issuer}/access_token",
                "code_challenge_methods_supported": ["S256"]}
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        if str(request.url) == "https://auth.example.test/.well-known/oauth-authorization-server/login/oauth":
            return httpx.Response(200, json=document)
        return httpx.Response(404)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await oauth._discover_authorization_server(client, issuer)

    found = asyncio.run(run())
    assert found is not None and found["token_endpoint"].endswith("/access_token"), asked


# --- tokens: refresh once, never anonymously, never silently ------------------------------------

def _oauth_connector(expires_in: float, *, refresh: str | None = "r1") -> dict:
    token_set = {"accessToken": "old", "refreshToken": refresh,
                 "expiresAt": time.time() + expires_in,
                 "tokenEndpoint": "https://auth.example.test/token", "clientId": "c1",
                 "clientSecret": None, "resource": None}
    connector = store.add_connector(
        type="mcp", label="Svc",
        config={"connectFlow": {"kind": "oauth_dcr", "url": "https://svc.example.test/mcp"}})
    save_secret(f"connoauth_{connector['id']}", json.dumps(token_set))
    store.update_connector(connector["id"], {
        "config": {"secretRef": f"connoauth_{connector['id']}"},
        "status": {"state": "working", "checkedAt": None, "detail": None}})
    return store.get_connector(connector["id"])


def _patch_token_endpoint(monkeypatch, handler):
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(oauth.httpx, "AsyncClient", factory)


def test_two_simultaneous_calls_refresh_an_expiring_token_once(monkeypatch):
    """A refresh token that works once (rotation) must not be spent twice."""
    connector = _oauth_connector(expires_in=-5)
    posts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        posts.append(body)
        if "refresh_token=r1" in body and len(posts) == 1:
            return httpx.Response(200, json={"access_token": "new", "refresh_token": "r2",
                                             "expires_in": 3600})
        return httpx.Response(400, json={"error": "invalid_grant"})  # r1 is spent

    _patch_token_endpoint(monkeypatch, handler)

    async def both():
        return await asyncio.gather(oauth.get_access_token(connector["id"]),
                                    oauth.get_access_token(connector["id"]))

    first = asyncio.run(both())
    assert first == ["new", "new"]
    assert len(posts) == 1
    assert json.loads(get_secret(connector["config"]["secretRef"]))["refreshToken"] == "r2"


def test_a_refresh_that_fails_marks_the_connector_for_reconnecting(monkeypatch):
    connector = _oauth_connector(expires_in=-5)
    _patch_token_endpoint(monkeypatch, lambda request: httpx.Response(400, json={"error": "invalid_grant"}))

    with pytest.raises(mcp_client.AuthRejected) as caught:
        mcp_client.fetch_tools(connector["config"], connector_id=connector["id"])

    assert "reconnect" in str(caught.value).lower()
    status = store.get_connector(connector["id"])["status"]
    assert status["state"] == "error" and "reconnect" in status["detail"].lower()


def test_a_connection_that_lost_its_token_is_never_called_anonymously(monkeypatch):
    """Resolving the token used to swallow its failure and send the request with no
    Authorization header, so the person was told the *server* refused their sign-in."""
    connector = _oauth_connector(expires_in=3600)
    save_secret(connector["config"]["secretRef"], "")  # the stored token vanished
    with pytest.raises(mcp_client.AuthRejected):
        mcp_client.fetch_tools(connector["config"], connector_id=connector["id"])


# --- "connected" is proven by reading the tools, and a refusal at call time is surfaced ----------

def test_a_server_that_refuses_the_token_is_not_left_saying_working():
    with StubMcpHttp(token="the-right-token") as stub:
        save_secret("conn_x", "a-wrong-token")
        connector = store.add_connector(
            type="mcp", label="Notes",
            config={"connectFlow": {"kind": "token", "url": stub.url}, "secretRef": "conn_x"})
        store.update_connector(connector["id"], {"status": {"state": "working", "checkedAt": None,
                                                            "detail": None}})
        with pytest.raises(mcp_client.AuthRejected):
            capabilities.refresh_tools(connector["id"])
    status = store.get_connector(connector["id"])["status"]
    assert status["state"] == "error"
    assert "connect" in status["detail"].lower()


def test_signed_in_but_unreadable_is_an_error_not_working(monkeypatch):
    from jarvis.routes import connectors as routes

    save_secret("conn_z", "a-token")
    connector = store.add_connector(type="mcp", label="Svc", config={
        "connectFlow": {"kind": "token", "url": "http://127.0.0.1:9/mcp"}, "secretRef": "conn_z"})
    store.update_connector(connector["id"], {"status": {"state": "working", "checkedAt": None,
                                                        "detail": None}})
    monkeypatch.setattr(routes, "_sync", lambda: None)

    done = []
    original = routes._record_discovery_failure
    monkeypatch.setattr(routes, "_record_discovery_failure",
                        lambda *a, **k: (original(*a, **k), done.append(1)))
    routes._discover_in_background(connector["id"])
    for _ in range(100):
        if done:
            break
        time.sleep(0.1)
    status = store.get_connector(connector["id"])["status"]
    assert status["state"] == "error"
    assert "couldn't read its tools" in status["detail"]


def test_a_tool_call_the_server_refuses_for_its_sign_in_marks_the_connector():
    with StubMcpHttp(token="the-right-token") as stub:
        save_secret("conn_y", "a-wrong-token")
        connector = store.add_connector(
            type="mcp", label="Notes",
            config={"connectFlow": {"kind": "token", "url": stub.url}, "secretRef": "conn_y",
                    "tools": [{"name": "echo", "description": "x", "parameters": {"type": "object"}}]})
        store.update_connector(connector["id"], {"status": {"state": "working", "checkedAt": None,
                                                            "detail": None}})
        answer = capabilities._dispatch(connector["id"], "mcp", "echo", "Notes__echo", {})
    assert answer["ok"] is False and "connect" in answer["error"].lower()
    assert store.get_connector(connector["id"])["status"]["state"] == "error"


# --- the wording of a failure -------------------------------------------------------------------

def test_a_plain_os_error_from_a_remote_call_is_not_called_unreachable():
    err = PermissionError("certificate verify failed")
    text = mcp_client._plain_failure(err, [], "https://svc.example.test/mcp")
    assert "reach" not in text and "certificate" in text


# --- the page after approving ---------------------------------------------------------------------

def test_a_failed_connection_stays_on_screen_and_cannot_inject_markup():
    from jarvis.routes.connectors import _callback_page

    page = _callback_page({"ok": False, "error": "<script>alert(1)</script> denied"})
    assert "window.close" not in page
    assert "<script>" not in page and "&lt;script&gt;" in page and "Could not connect" in page


def test_a_successful_connection_closes_its_tab():
    from jarvis.routes.connectors import _callback_page

    assert "window.close" in _callback_page({"ok": True, "connectorId": "x"})


# --- a pasted token: the way in when a service has no automatic sign-in ---------------------------

@pytest.fixture
def api(scratch):
    from starlette.testclient import TestClient

    from jarvis.main import create_app

    return TestClient(create_app())


def _github_like(stub) -> dict:
    """A catalogue-style connector: browser sign-in that needs a Client ID Jarvis lacks."""
    return store.add_connector(type="mcp", label="Notes", config={
        "connectFlow": {"kind": "oauth_guided", "url": stub.url,
                        "manualClient": {"reason": "unsupported", "message": "needs a Client ID"}}})


def test_a_pasted_token_connects_and_is_proven_by_reading_the_tools(api):
    with StubMcpHttp(token="ghp_realtoken") as stub:
        connector = _github_like(stub)
        answer = api.post(f"/api/connectors/{connector['id']}/token",
                          json={"token": "Bearer ghp_realtoken"})
        assert answer.status_code == 200, answer.text
        assert answer.json()["tools"] == 2
        saved = store.get_connector(connector["id"])
        assert saved["status"]["state"] == "working"
        assert saved["config"]["connectFlow"]["kind"] == "token"
        assert get_secret(saved["config"]["secretRef"]) == "ghp_realtoken"
        # the token is never handed back to the screen
        assert "ghp_realtoken" not in json.dumps(answer.json())


def test_a_wrong_pasted_token_is_an_error_the_person_sees(api):
    with StubMcpHttp(token="ghp_realtoken") as stub:
        connector = _github_like(stub)
        answer = api.post(f"/api/connectors/{connector['id']}/token", json={"token": "nope"})
    assert answer.status_code == 400
    assert store.get_connector(connector["id"])["status"]["state"] == "error"


def test_an_empty_token_is_refused_without_touching_the_connector(api):
    with StubMcpHttp(token="t") as stub:
        connector = _github_like(stub)
        assert api.post(f"/api/connectors/{connector['id']}/token", json={"token": "  "}).status_code == 400
    assert "secretRef" not in store.get_connector(connector["id"])["config"]


def test_signing_in_after_a_pasted_token_restores_the_sign_in_kind(api, monkeypatch):
    """Otherwise the OAuth token set would be sent as a bare token."""
    with StubMcpHttp(token="ghp_realtoken") as stub:
        connector = _github_like(stub)
        api.post(f"/api/connectors/{connector['id']}/token", json={"token": "ghp_realtoken"})
        seen = {}

        async def fake_start(connector_id, url, **kw):
            seen["kind"] = store.get_connector(connector_id)["config"]["connectFlow"]["kind"]
            return {"authUrl": "https://example.test/authorize"}

        monkeypatch.setattr(oauth, "start_connect", fake_start)
        assert api.post(f"/api/connectors/{connector['id']}/connect", json={}).status_code == 200
    assert seen["kind"] == "oauth_guided"


# --- a slow or briefly unreachable server (Composio answers in 5-17 s, and once did not at all) ---

def test_reading_the_tool_list_is_tried_twice_for_a_transient_failure(monkeypatch):
    calls = []

    def flaky(config, work, *, connector_id=None):
        calls.append(1)
        async def fail_then_ok():
            if len(calls) == 1:
                raise mcp_client.TransientFailure("The app's server took too long to answer.")
            return [{"name": "t", "description": "d", "parameters": {"type": "object"}}]
        return fail_then_ok()

    monkeypatch.setattr(mcp_client, "_with_client", flaky)
    monkeypatch.setattr(mcp_client, "RETRY_PAUSE_S", 0.0)
    assert [t["name"] for t in mcp_client.fetch_tools({"connectFlow": {"url": "http://x"}})] == ["t"]
    assert len(calls) == 2


def test_a_tool_call_is_never_repeated_behind_the_persons_back(monkeypatch):
    calls = []

    def always_slow(config, work, *, connector_id=None):
        calls.append(1)
        async def fail():
            raise mcp_client.TransientFailure("The app's server took too long to answer.")
        return fail()

    monkeypatch.setattr(mcp_client, "_with_client", always_slow)
    with pytest.raises(mcp_client.TransientFailure):
        mcp_client.dispatch("send_it", {}, {"connectFlow": {"url": "http://x"}})
    assert len(calls) == 1


def test_a_refused_sign_in_is_not_retried(monkeypatch):
    calls = []

    def refused(config, work, *, connector_id=None):
        calls.append(1)
        async def fail():
            raise mcp_client.AuthRejected("reconnect it")
        return fail()

    monkeypatch.setattr(mcp_client, "_with_client", refused)
    with pytest.raises(mcp_client.AuthRejected):
        mcp_client.fetch_tools({"connectFlow": {"url": "http://x"}})
    assert len(calls) == 1


# --- discovery must survive one bad moment on the network ------------------------------------------

def test_a_metadata_lookup_that_fails_once_is_tried_again(monkeypatch):
    monkeypatch.setattr(oauth, "RETRY_PAUSE_S", 0.0)
    answers = iter([httpx.Response(503), httpx.ConnectError("blip"), httpx.Response(200, json={"issuer": "x"})])

    def handler(request):
        nxt = next(answers)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await oauth._try_fetch_json(client, "https://x.test/.well-known/y")

    assert asyncio.run(run()) == {"issuer": "x"}


def test_a_missing_document_is_not_retried(monkeypatch):
    monkeypatch.setattr(oauth, "RETRY_PAUSE_S", 0.0)
    asked = []

    def handler(request):
        asked.append(1)
        return httpx.Response(404)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await oauth._try_fetch_json(client, "https://x.test/.well-known/y")

    assert asyncio.run(run()) is None and len(asked) == 1
