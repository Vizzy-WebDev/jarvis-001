"""The browser connector, against a real page served over real HTTP.

Headless here, because there is no display in this container — but the code
under test is the same code the visible window runs: navigation, reading,
clicking and typing all go through the page itself, which is the whole reason
this is a connector rather than something the desktop loop pushes a mouse at.
What is NOT verified here is the window: that it opens, that it is visible, that
it is its own profile on the user's machine. That needs a desktop, and it is on
the self-check.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from jarvis.connectors import browser_connector, capabilities, store
from jarvis.capabilities import CapabilityRegistry
from jarvis.db import reset_for_tests as reset_db
from jarvis.webrender import browser_path

PAGE = b"""<!doctype html><html><head><title>A Test Page</title></head><body>
<h1>Hello from the connector test</h1>
<p id="status">nothing clicked yet</p>
<button id="go" onclick="document.getElementById('status').textContent='the button was clicked'">
Click me</button>
<input id="field" />
<script>
  document.getElementById('field').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') {
      document.getElementById('status').textContent = 'submitted: ' + this.value;
    }
  });
</script>
</body></html>"""

#: A page that ships no content and builds itself — the case a plain fetch
#: cannot read at all.
BUILT_BY_SCRIPT = b"""<!doctype html><html><head><title>Built</title></head><body>
<div id="root"></div>
<script>document.getElementById('root').textContent = 'this text only exists after running';
</script></body></html>"""


@pytest.fixture(autouse=True)
def _isolated(scratch):
    reset_db()
    yield
    browser_connector.close_for_tests()
    reset_db()


@pytest.fixture
def site():
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_GET(self):
            body = BUILT_BY_SCRIPT if self.path.startswith("/built") else PAGE
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    yield f"http://{host}:{port}"
    server.shutdown()
    server.server_close()


needs_browser = pytest.mark.skipif(browser_path() is None,
                                   reason="no browser installed to drive")


# --- what it is, without opening anything ------------------------------------

def test_the_browser_is_one_of_jarvis_own_abilities_not_something_added():
    """A singleton, like the file allowlist: there is one browser, so its tools
    keep their plain names rather than being prefixed with a label."""
    connector = store.get_or_create_singleton("browser")
    assert connector["type"] == "browser" and connector["label"] == "Browser"

    registry = CapabilityRegistry()
    names = capabilities.sync(registry)
    assert "browser_navigate" in names and "browser_read_page" in names
    assert not any(name.startswith("browser__") for name in names)


def test_navigating_is_treated_as_ordinary_and_typing_is_not():
    """These declarations are ours, so their risk is declared rather than
    guessed from words. Clicking or typing on an arbitrary page can pay, send or
    post, and there is no overlay and no watching user here to stop it — unlike
    the control loop, where the same actions are merely notable."""
    store.get_or_create_singleton("browser")
    registry = CapabilityRegistry()
    capabilities.sync(registry)
    from jarvis.capabilities import Risk

    assert registry.get("browser_navigate").risk is Risk.LOW
    assert registry.get("browser_type").risk is Risk.MEDIUM


def test_the_profile_lives_in_the_data_directory(scratch):
    """A browser profile is large, machine-owned state. The original put one of
    these — 145MB of it — into the real data folder during a scratch test run,
    which is exactly what deriving the path from data_dir() prevents."""
    assert browser_connector._session.profile_dir().startswith(str(scratch.data_dir))


def test_acting_before_opening_a_page_says_so_rather_than_opening_one():
    result = browser_connector.dispatch("browser_click", {"selector": "#go"}, {})
    assert result["ok"] is False and "browser_navigate first" in result["error"]


def test_an_unknown_action_is_refused():
    result = browser_connector.dispatch("browser_dance", {}, {})
    assert result["ok"] is False


# --- against a real page -----------------------------------------------------

@needs_browser
def test_reading_clicking_and_typing_on_a_real_page(site):
    config = {"headless": True}                 # no display in this container

    opened = browser_connector.dispatch("browser_navigate", {"url": site}, config)
    assert opened["ok"] is True
    assert opened["title"] == "A Test Page"
    assert "Hello from the connector test" in opened["text"]

    read = browser_connector.dispatch("browser_read_page", {}, config)
    assert "nothing clicked yet" in read["text"]

    clicked = browser_connector.dispatch("browser_click", {"selector": "#go"}, config)
    assert clicked["ok"] is True
    assert "the button was clicked" in clicked["text"], "the real page really changed"

    typed = browser_connector.dispatch(
        "browser_type", {"selector": "#field", "text": "an invoice number", "submit": True},
        config)
    assert "submitted: an invoice number" in typed["text"]

    assert browser_connector.dispatch("browser_close", {}, config) == {"ok": True,
                                                                       "closed": True}


@needs_browser
def test_a_page_that_builds_itself_is_read_by_running_it(site):
    """The case a plain fetch cannot answer at all: the text is not in the
    markup, it is made by the page."""
    result = browser_connector.dispatch("browser_navigate", {"url": f"{site}/built"},
                                        {"headless": True})
    assert "this text only exists after running" in result["text"]


def test_an_address_without_a_scheme_gets_one():
    """"example.com" is an address a person types. https, not http: defaulting
    to the unencrypted one because it is easier to reach is not a default worth
    having."""
    asked = {}

    class OnlyRecords:
        is_open = True

        def page(self, **_kwargs):
            return self

        def goto(self, url, **_kwargs):
            asked["url"] = url

        def title(self):
            return ""

        @property
        def url(self):
            return asked["url"]

        def evaluate(self, _script):
            return ""

    browser_connector.dispatch("browser_navigate", {"url": "example.com"}, {},
                               session=OnlyRecords())
    assert asked["url"] == "https://example.com"


@needs_browser
def test_it_goes_through_the_same_permission_gate_as_any_connector(site):
    """Nothing special: a connector tool the user turned off is absent, and one
    they left on runs through the ordinary registry."""
    connector = store.get_or_create_singleton("browser")
    store.update_connector(connector["id"], {"config": {"headless": True}})
    registry = CapabilityRegistry()
    capabilities.sync(registry)

    result = registry.get("browser_navigate").handler(url=site)
    assert result["ok"] is True and result["title"] == "A Test Page"

    store.set_tool_permission(connector["id"], "browser_navigate", "deny")
    registry = CapabilityRegistry()
    assert "browser_navigate" not in capabilities.sync(registry)


def test_no_browser_installed_is_said_plainly(monkeypatch):
    monkeypatch.setattr(browser_connector, "browser_path", lambda: None)
    with pytest.raises(browser_connector.NoBrowser) as refused:
        browser_connector._Session().page()
    assert "no browser installed" in str(refused.value)
