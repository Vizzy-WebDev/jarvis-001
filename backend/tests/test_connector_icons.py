"""Every connector's real, current logo — icons.py, previously untested.

The one thing worth verifying with a REAL fetch is that a genuine HTTP
response (a real content-type header, real bytes, a real size) round-trips
into a correct `data:` URI — everything else (caching, staleness, curated
preference, cache-only reads) is pure logic around that one fetch and is
tested by substituting it, the same discipline `test_connector_oauth.py`
uses for a slower, heavier round trip.
"""

from __future__ import annotations

import base64
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest

from jarvis.connectors import icons


@pytest.fixture(autouse=True)
def _isolate(scratch):
    yield


# --- pure helpers ---------------------------------------------------------------

def test_host_of_reads_a_url_hostname():
    assert icons.host_of("https://mcp.notion.com/mcp?x=1") == "mcp.notion.com"
    assert icons.host_of("not a url") is None
    assert icons.host_of("http://127.0.0.1:3000/mcp") is None  # an address, not a name


def test_apex_of_drops_a_subdomain():
    assert icons.apex_of("mcp.notion.com") == "notion.com"
    assert icons.apex_of("notion.com") is None  # already the apex


# --- caching, staleness, curated preference (a stubbed fetch) -------------------

def test_a_fresh_resolve_is_cached_and_not_fetched_again(scratch, monkeypatch):
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return "data:image/png;base64,aGVsbG8="

    monkeypatch.setattr(icons, "_fetch", fake_fetch)
    first = icons.icon_for("mcp.notion.com")
    second = icons.icon_for("mcp.notion.com")
    assert first == second == "data:image/png;base64,aGVsbG8="
    assert len(calls) == 1


def test_refresh_false_reads_the_cache_only_and_never_fetches(scratch, monkeypatch):
    calls = []
    monkeypatch.setattr(icons, "_fetch", lambda url: calls.append(url) or "data:image/x-icon;base64,eA==")
    assert icons.icon_for("example.com", refresh=False) is None  # nothing cached yet
    assert calls == []


def test_a_curated_key_is_tried_before_the_generic_lookup(scratch, monkeypatch):
    seen = []

    def fake_fetch(url):
        seen.append(url)
        return "data:image/svg+xml;base64,aGVsbG8=" if url == icons.CURATED["gmail"] else None

    monkeypatch.setattr(icons, "_fetch", fake_fetch)
    found = icons.icon_for("mail.google.com", curated_key="gmail")
    assert found == "data:image/svg+xml;base64,aGVsbG8="
    assert seen[0] == icons.CURATED["gmail"]  # curated tried first, and it won


def test_a_stale_entry_is_re_fetched_a_fresh_one_is_not(scratch, monkeypatch):
    cache = icons._cache()
    cache["icons"]["notion.com"] = {"dataUri": "data:image/png;base64,b2xk",
                                    "fetchedAt": time.time() - icons.REFRESH_AFTER_S - 1}
    icons.write_json(icons.FILE, cache)

    calls = []
    monkeypatch.setattr(icons, "_fetch", lambda url: calls.append(url) or "data:image/png;base64,bmV3")
    assert icons.icon_for("notion.com") == "data:image/png;base64,bmV3"
    assert calls  # the stale entry triggered a real re-fetch


def test_a_recent_failure_is_not_retried_immediately(scratch, monkeypatch):
    cache = icons._cache()
    cache["icons"]["nologo.example"] = {"dataUri": None, "fetchedAt": time.time()}
    icons.write_json(icons.FILE, cache)

    calls = []
    monkeypatch.setattr(icons, "_fetch", lambda url: calls.append(url) or None)
    assert icons.icon_for("nologo.example") is None
    assert calls == []  # too soon to retry a known-recent failure


# --- a real HTTP round trip: content-type + size checks, real bytes -------------

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class _IconHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # noqa: A003
        pass

    def do_GET(self):  # noqa: N802
        if self.path == "/good.png":
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            self.wfile.write(_PNG_BYTES)
        elif self.path == "/wrong-type.txt":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"not an image")
        elif self.path == "/too-big.png":
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            self.wfile.write(b"\x00" * (icons.MAX_BYTES + 1))
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture
def icon_server():
    server = HTTPServer(("127.0.0.1", 0), _IconHandler)
    Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_a_real_image_response_becomes_a_correct_data_uri(icon_server):
    found = icons._fetch(f"{icon_server}/good.png")
    assert found == f"data:image/png;base64,{base64.b64encode(_PNG_BYTES).decode()}"


def test_a_non_image_content_type_is_refused(icon_server):
    assert icons._fetch(f"{icon_server}/wrong-type.txt") is None


def test_an_oversized_body_is_refused_even_with_an_image_content_type(icon_server):
    assert icons._fetch(f"{icon_server}/too-big.png") is None


def test_a_404_is_refused_not_raised(icon_server):
    assert icons._fetch(f"{icon_server}/does-not-exist.png") is None
