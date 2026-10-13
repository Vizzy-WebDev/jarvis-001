"""The OAuth return trip must get past the request guard — and nothing else may.

When someone approves Jarvis on a provider's page (Canva, ChatCut, ...), the provider
redirects their browser to `/api/connectors/oauth/callback`. A browser labels that
navigation `Sec-Fetch-Site: cross-site`, which the guard refuses everywhere else, so
every connector added after the guard existed ended on `{"ok": false, "error": "That
request came from a page outside Jarvis..."}`. The tests that existed called the
route with no headers at all, so none of them could see it.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

CALLBACK = "/api/connectors/oauth/callback?code=x&state=nothing-started-this"

#: What a real browser sends when a provider's page redirects it to Jarvis.
PROVIDER_REDIRECT = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate",
                     "Sec-Fetch-Dest": "document"}


@pytest.fixture
def client(scratch):
    from jarvis.db import reset_for_tests
    from jarvis.main import create_app

    reset_for_tests()
    yield TestClient(create_app())
    reset_for_tests()


def test_the_providers_redirect_reaches_the_callback(client):
    response = client.get(CALLBACK, headers=PROVIDER_REDIRECT)
    assert response.status_code == 200
    # It got to the route: the answer is about the (unknown) sign-in, not the guard.
    assert "outside Jarvis" not in response.text
    assert "invalid or was already used" in response.text


@pytest.mark.parametrize("headers", [
    # A page Jarvis made, in its sandboxed viewer, navigating a frame or a script's
    # fetch to the callback: not a top-level document navigation.
    {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "iframe"},
    {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"},
    {"Sec-Fetch-Site": "cross-site"},
    # An opaque origin is refused whatever else it says.
    {**PROVIDER_REDIRECT, "Origin": "null"},
])
def test_only_a_real_page_navigation_is_let_through(client, headers):
    assert client.get(CALLBACK, headers=headers).status_code == 403


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/chat/stream?message=hi"),
    ("GET", "/api/artifacts"),
    ("GET", "/api/connectors"),
    ("POST", "/api/connectors/oauth/callback?code=x&state=y"),
    ("GET", "/api/connectors/oauth/redirect-uri"),
])
def test_the_exemption_is_the_callback_alone_and_get_alone(client, method, path):
    assert client.request(method, path, headers=PROVIDER_REDIRECT).status_code == 403
