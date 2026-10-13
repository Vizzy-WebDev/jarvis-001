"""Jarvis's browser over HTTP: the same sessions the agent uses, and the Take control page.

Through FastAPI's real test client and the real app, with the fixture site behind a
host whose destination checks are the production ones. The claims: a window can be
opened, read, driven and closed over HTTP; the picture of a page is only ever served
as a download (outside content never renders inline in Jarvis's own origin); the
Take control page only ever shows a picture and takes no markup from the page it
shows; and a request another site started never reaches any of it.
"""

from __future__ import annotations

import pytest
from browser_site import BASE, FixtureSite
from fastapi.testclient import TestClient

from jarvis import assembly
from jarvis.browser import set_host_for_tests
from jarvis.browser.console import console_page
from jarvis.browser.locate import find_browser
from jarvis.db import reset_for_tests as reset_db
from jarvis.main import create_app

needs_browser = pytest.mark.skipif(find_browser() is None, reason="no Chrome or Edge to drive")

SID = "0a7e5d11-6a72-4a76-8a1c-0000000000c1"


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    assembly.reset_for_tests()
    yield
    set_host_for_tests(None)
    assembly.reset_for_tests()
    reset_db()


@pytest.fixture
def client():
    return TestClient(create_app())


@pytest.fixture
def site(tmp_path):
    fixture = FixtureSite()
    set_host_for_tests(fixture.host(tmp_path / "sessions"))
    yield fixture
    fixture.close()


# --- without a browser ----------------------------------------------------------

def test_status_says_whether_a_browser_can_be_opened_without_opening_one(client):
    body = client.get("/api/browsers/status").json()
    assert body["ok"] is True and isinstance(body["available"], bool)
    assert body["started"] is False
    if not body["available"]:
        assert body["reason"]


def test_the_console_is_only_made_for_a_real_session_id(client):
    assert client.get("/api/browsers/not-a-uuid/console").status_code == 400
    assert client.get(f"/api/browsers/{SID}/console").status_code == 404
    with pytest.raises(Exception):
        console_page("not-a-uuid")


def test_the_console_page_is_a_fixed_template_with_a_strict_policy(client, tmp_path):
    fixture = FixtureSite()
    try:
        set_host_for_tests(fixture.host(tmp_path / "sessions"))
        # A session that exists on disk but is not running: listed as closed.
        folder = tmp_path / "sessions" / SID
        folder.mkdir(parents=True)
        (folder / "session.json").write_text(
            '{"id": "%s", "title": "<script>alert(1)</script>", "url": "%s/", '
            '"status": "closed", "updatedAt": "2026-10-11T00:00:00Z"}' % (SID, BASE), "utf-8")
        set_host_for_tests(fixture.host(tmp_path / "sessions"))
        page = client.get(f"/api/browsers/{SID}/console")
        assert page.status_code == 200
        policy = page.headers["content-security-policy"]
        assert "default-src 'none'" in policy and "frame-ancestors 'none'" in policy
        assert "connect-src 'self'" in policy and "img-src blob:" in policy
        assert page.headers["x-content-type-options"] == "nosniff"
        # The remote page's own title never reaches this document at all: it is fetched
        # as JSON and set with textContent.
        assert "<script>alert(1)</script>" not in page.text
        assert "textContent" in page.text and "innerHTML" not in page.text
        assert f'const ID="{SID}"' in page.text
    finally:
        fixture.close()


def test_a_closed_window_answers_409_for_its_picture_and_404_for_a_stranger(client, tmp_path):
    fixture = FixtureSite()
    try:
        folder = tmp_path / "sessions" / SID
        folder.mkdir(parents=True)
        (folder / "session.json").write_text(
            '{"id": "%s", "title": "T", "url": "%s/", "status": "active", '
            '"updatedAt": "2026-10-11T00:00:00Z"}' % (SID, BASE), "utf-8")
        set_host_for_tests(fixture.host(tmp_path / "sessions"))
        closed = client.get(f"/api/browsers/{SID}/preview")
        assert closed.status_code == 409 and closed.json()["code"] == "SESSION_CLOSED"
        stranger = client.get("/api/browsers/0a7e5d11-6a72-4a76-8a1c-0000000000ff/preview")
        assert stranger.status_code == 404
        info = client.get(f"/api/browsers/{SID}").json()
        assert info["status"] == "closed" and info["running"] is False
    finally:
        fixture.close()


def test_a_request_another_site_started_never_reaches_the_browser(client):
    """`request_guard`: a browser labels a request from a sandboxed page (an artifact
    Jarvis made) `Origin: null`, and one another site started `cross-site`."""
    for headers in ({"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}):
        assert client.get("/api/browsers", headers=headers).status_code == 403
        assert client.post("/api/browsers", json={"url": "https://example.com"},
                           headers=headers).status_code == 403


def test_opening_something_that_is_not_a_web_page_is_refused_plainly(client, site):
    for url in ("http://127.0.0.1:3000/api/health", "file:///C:/Windows/win.ini",
                "http://localhost/"):
        refused = client.post("/api/browsers", json={"url": url})
        assert refused.status_code == 400 and refused.json()["code"] == "BLOCKED_URL"
        assert "off limits" in refused.json()["error"]
    assert client.post("/api/browsers", json={}).status_code == 400
    assert client.get("/api/browsers").json() == {"ok": True, "sessions": []}


# --- with one ---------------------------------------------------------------------

@needs_browser
def test_a_window_is_opened_read_driven_pictured_and_closed_over_http(client, site):
    opened = client.post("/api/browsers", json={"url": f"{BASE}/"})
    assert opened.status_code == 201
    session = opened.json()["session"]
    sid = session["id"]
    assert session["status"] == "active" and session["title"] == "Fixture Home"

    read = client.get(f"/api/browsers/{sid}/read", params={"elements": "true"}).json()
    assert "Hello from the fixture site" in read["text"]
    assert any(e["label"] == "Click me" for e in read["elements"])

    shot = client.get(f"/api/browsers/{sid}/preview")
    assert shot.status_code == 200 and shot.content[:4] == b"\x89PNG"
    # Outside content is only ever served as a download, never inline.
    assert shot.headers["content-disposition"].startswith("attachment")
    assert shot.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in shot.headers["content-security-policy"]
    assert shot.headers["cache-control"] == "no-store"

    moved = client.post(f"/api/browsers/{sid}/navigate", json={"url": f"{BASE}/two"}).json()
    assert moved["session"]["title"] == "Second"
    assert client.post(f"/api/browsers/{sid}/input",
                       json={"type": "scroll", "deltaY": 100}).json()["ok"] is True
    bad = client.post(f"/api/browsers/{sid}/input", json={"type": "click", "x": -4, "y": 1})
    assert bad.status_code == 400 and bad.json()["code"] == "INVALID_INPUT"

    console = client.get(f"/api/browsers/{sid}/console")
    assert console.status_code == 200 and "text/html" in console.headers["content-type"]

    assert client.post(f"/api/browsers/{sid}/close").json()["session"]["status"] == "closed"
    assert client.get(f"/api/browsers/{sid}").json()["running"] is False
    assert client.get(f"/api/browsers/{sid}/preview").status_code == 409

    # "Open it again" brings the same window back, at the page it was on.
    reopened = client.post(f"/api/browsers/{sid}/open", json={}).json()["session"]
    assert reopened["status"] == "active" and reopened["url"] == f"{BASE}/two"


@needs_browser
def test_the_window_the_agent_uses_is_the_one_the_person_sees(client, site):
    from jarvis.connectors import browser_connector

    browser_connector.dispatch("browser_navigate", {"url": f"{BASE}/two"}, {})
    sid = browser_connector.AGENT_SESSION_ID
    listed = client.get("/api/browsers").json()["sessions"]
    assert [s["id"] for s in listed] == [sid]
    assert client.get(f"/api/browsers/{sid}").json()["running"] is True
    assert client.get(f"/api/browsers/{sid}/preview").content[:4] == b"\x89PNG"
