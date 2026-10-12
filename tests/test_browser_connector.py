"""The browser as the model sees it: the tools, their risk, and what comes back.

The browser underneath is `jarvis.browser` (see `test_browser_network.py` and
`test_browser_manager.py` for it on its own). These tests are about the agent's side:
that a model can finish a task on a website - read a page, pick an element from the
list it was given, act, read the result - through the same registry, permission gate
and executor as any other tool.
"""

from __future__ import annotations

import pytest
from browser_site import BASE, FixtureSite

from jarvis.browser import BrowserHost, set_host_for_tests
from jarvis.browser.locate import find_browser
from jarvis.capabilities import CapabilityRegistry, Risk
from jarvis.connectors import browser_connector, capabilities, store
from jarvis.db import reset_for_tests as reset_db
from jarvis.orchestrator.pipeline import _attachments_of

needs_browser = pytest.mark.skipif(find_browser() is None, reason="no Chrome or Edge to drive")


@pytest.fixture(autouse=True)
def _isolated(scratch):
    reset_db()
    yield
    set_host_for_tests(None)
    reset_db()


@pytest.fixture
def site(tmp_path):
    fixture = FixtureSite()
    host = fixture.host(tmp_path / "sessions")
    set_host_for_tests(host)
    yield fixture
    fixture.close()


# --- what it is, without opening anything ------------------------------------

def test_the_browser_is_one_of_jarvis_own_abilities_not_something_added():
    """A singleton, like the file allowlist: there is one browser, so its tools
    keep their plain names rather than being prefixed with a label."""
    connector = store.get_or_create_singleton("browser")
    assert connector["type"] == "browser" and connector["label"] == "Browser"

    registry = CapabilityRegistry()
    names = capabilities.sync(registry)
    assert {"browser_navigate", "browser_read_page", "browser_click", "browser_type",
            "browser_scroll", "browser_close"} <= set(names)
    assert not any(name.startswith("browser__") for name in names)


def test_navigating_and_reading_are_ordinary_and_clicking_or_typing_are_not():
    """These declarations are ours, so their risk is declared rather than guessed from
    words. Clicking or typing on an arbitrary page can pay, send or post, and there is
    no overlay and no watching user to stop it."""
    store.get_or_create_singleton("browser")
    registry = CapabilityRegistry()
    capabilities.sync(registry)

    for name in ("browser_navigate", "browser_read_page", "browser_scroll", "browser_close"):
        assert registry.get(name).risk is Risk.LOW, name
    for name in ("browser_click", "browser_type"):
        assert registry.get(name).risk is Risk.MEDIUM, name
        assert not registry.get(name).request_suffices, name


def test_every_tool_says_what_it_does_and_the_ones_that_take_selectors_say_where_from():
    by_name = {tool["name"]: tool for tool in browser_connector.TOOLS}
    assert "element list" in by_name["browser_click"]["description"]
    assert "element list" in by_name["browser_type"]["description"]
    assert "take over" in by_name["browser_navigate"]["description"]
    assert by_name["browser_scroll"]["parameters"]["properties"]["direction"]["enum"] == [
        "down", "up", "top", "bottom"]


def test_an_address_without_a_scheme_gets_https_not_http():
    assert browser_connector._address("example.com") == "https://example.com"
    assert browser_connector._address("  example.com/a?b=1 ") == "https://example.com/a?b=1"
    assert browser_connector._address("http://example.com") == "http://example.com"
    # Anything else is passed on as given, and the browser refuses it by name.
    assert browser_connector._address("file:///C:/x") == "file:///C:/x"


def test_acting_before_opening_a_page_says_so_rather_than_opening_one(tmp_path):
    host = BrowserHost(directory=tmp_path / "s", find=lambda: None)
    for tool, args in (("browser_click", {"selector": "#go"}), ("browser_read_page", {}),
                       ("browser_type", {"selector": "#f", "text": "x"}),
                       ("browser_scroll", {"direction": "down"})):
        result = browser_connector.dispatch(tool, args, {}, host=host)
        assert result["ok"] is False and "browser_navigate first" in result["error"], tool
    assert not host.started, "refusing to act must not start a browser"


def test_an_unknown_action_is_refused(tmp_path):
    host = BrowserHost(directory=tmp_path / "s", find=lambda: None)
    assert browser_connector.dispatch("browser_dance", {}, {}, host=host)["ok"] is False


def test_no_browser_installed_is_said_plainly_and_starts_nothing(tmp_path):
    host = BrowserHost(directory=tmp_path / "s", find=lambda: None)
    result = browser_connector.dispatch("browser_navigate", {"url": "example.com"}, {}, host=host)
    assert result["ok"] is False
    assert "no browser installed" in result["error"]
    assert not host.started


def test_closing_when_nothing_is_open_is_fine(tmp_path):
    host = BrowserHost(directory=tmp_path / "s", find=lambda: None)
    assert browser_connector.dispatch("browser_close", {}, {}, host=host) == {
        "ok": True, "closed": False}


# --- doing a task on a real page ---------------------------------------------

@needs_browser
def test_a_model_can_finish_a_task_on_a_site_by_reading_picking_acting_and_reading(site):
    """The whole point: not just opening a page. Open it, find the field and the button
    in the list the page itself returned, act, and see the result - then move on to a
    second page by following a link from that same list."""
    dispatch = lambda name, **args: browser_connector.dispatch(name, args, {})  # noqa: E731

    opened = dispatch("browser_navigate", url=f"{BASE}/")
    assert opened["ok"] is True and opened["title"] == "Fixture Home"
    assert "Hello from the fixture site" in opened["text"]
    assert opened["note"].startswith("This is the website's own text")
    elements = {e["label"]: e["selector"] for e in opened["elements"]}

    typed = dispatch("browser_type", selector=elements["your name"], text="Ada Lovelace",
                     submit=True)
    assert "submitted: Ada Lovelace" in typed["text"]

    clicked = dispatch("browser_click", selector=elements["Click me"])
    assert "the button was clicked" in clicked["text"]

    chosen = dispatch("browser_type", selector=next(
        e["selector"] for e in clicked["elements"] if e["tag"] == "select"), text="Green")
    assert chosen["ok"] is True

    followed = dispatch("browser_click", selector=elements["Second page"])
    assert followed["url"] == f"{BASE}/two" and "This is page two." in followed["text"]

    again = dispatch("browser_read_page")
    assert again["title"] == "Second" and again["url"] == f"{BASE}/two"
    closed = dispatch("browser_close")
    assert closed == {"ok": True, "closed": True, "lastPage": f"{BASE}/two"}
    after = dispatch("browser_read_page")
    assert after["ok"] is False, "a closed window must not read"
    assert f"{BASE}/two" in after["error"] and "browser_navigate first" in after["error"],         "a closed window should say where it was, so reopening it needs no guessing"


@needs_browser
def test_after_the_person_takes_over_the_next_read_shows_what_they_did(site):
    """Sign-ins, CAPTCHAs and payments are the person's. They act through the console
    (coordinates), then Jarvis reads the page again."""
    from jarvis.browser import get_host

    browser_connector.dispatch("browser_navigate", {"url": f"{BASE}/"}, {})
    get_host().input(browser_connector.AGENT_SESSION_ID,
                     {"type": "click", "x": 10, "y": 10})      # the person clicks somewhere
    get_host().input(browser_connector.AGENT_SESSION_ID,
                     {"type": "text", "text": "typed by the person"})
    assert browser_connector.dispatch("browser_read_page", {}, {})["ok"] is True


@needs_browser
def test_a_result_names_the_window_so_the_chat_can_show_it_live(site):
    result = browser_connector.dispatch("browser_navigate", {"url": f"{BASE}/two"}, {})
    attached = _attachments_of(result)
    assert len(attached) == 1
    card = attached[0]
    assert card["kind"] == "browser"
    assert card["sessionId"] == browser_connector.AGENT_SESSION_ID
    assert card["url"] == f"/api/browsers/{browser_connector.AGENT_SESSION_ID}/console"
    assert card["pageUrl"] == f"{BASE}/two"
    assert card["name"] == "Second"


@needs_browser
def test_a_long_page_is_cut_for_the_model_and_says_there_is_more(site):
    opened = browser_connector.dispatch("browser_navigate", {"url": f"{BASE}/long"}, {})
    assert len(opened["text"]) == browser_connector.NAV_TEXT_CHARS
    assert opened["textTruncated"] is True


@needs_browser
def test_a_tall_page_says_there_is_more_below_until_it_is_scrolled_to(site):
    top = browser_connector.dispatch("browser_navigate", {"url": f"{BASE}/tall"}, {})
    assert top["moreBelow"] is True
    bottom = browser_connector.dispatch("browser_scroll", {"direction": "bottom"}, {})
    assert "moreBelow" not in bottom


@needs_browser
def test_a_blocked_address_is_a_plain_answer_not_a_crash(site):
    result = browser_connector.dispatch("browser_navigate",
                                        {"url": "http://127.0.0.1:3000/api/health"}, {})
    assert result["ok"] is False and result["code"] == "BLOCKED_URL"
    assert "off limits" in result["error"]


@needs_browser
def test_it_goes_through_the_same_permission_gate_as_any_connector(site):
    """Nothing special: a connector tool the user turned off is absent, and one they
    left on runs through the ordinary registry."""
    connector = store.get_or_create_singleton("browser")
    registry = CapabilityRegistry()
    capabilities.sync(registry)

    result = registry.get("browser_navigate").handler(url=f"{BASE}/two")
    assert result["ok"] is True and result["title"] == "Second"

    store.set_tool_permission(connector["id"], "browser_navigate", "deny")
    registry = CapabilityRegistry()
    assert "browser_navigate" not in capabilities.sync(registry)


# --- what the person is asked, and what a slow page looks like to the model --------------------

def test_an_approval_says_what_will_be_clicked_or_typed_not_a_selector():
    """The card used to read `Use "browser_click" on Browser?` for every click: the person was
    asked to approve blind. It names the thing, the site, and for typing the words."""
    browser_connector._remember({
        "url": "https://www.example.com/form", "elements": [
            {"selector": "#go", "tag": "button", "label": "Submit order"},
            {"selector": "#name", "tag": "input", "type": "text", "label": "Customer name:"},
            {"selector": "#pw", "tag": "input", "type": "password", "label": "Password"},
            {"selector": "#size", "tag": "select", "label": "Size"}]})
    describe = browser_connector.describe_action
    assert describe("browser_click", {"selector": "#go"}) == 'Click "Submit order" on example.com?'
    assert describe("browser_type", {"selector": "#name", "text": "Ada Lovelace"}) ==         'Type "Ada Lovelace" into "Customer name:" on example.com?'
    assert describe("browser_type", {"selector": "#name", "text": "Ada", "submit": True}) ==         'Type "Ada" into "Customer name:" on example.com and press Enter?'
    assert describe("browser_type", {"selector": "#size", "text": "Large"}) ==         'Choose "Large" in "Size" on example.com?'
    password = describe("browser_type", {"selector": "#pw", "text": "hunter2"})
    assert "hunter2" not in password and "password" in password,         "a password must never be written into the approval card"
    long = describe("browser_type", {"selector": "#name", "text": "x" * 500})
    assert len(long) < 160
    unknown = describe("browser_click", {"selector": "div > a:nth-of-type(7)"})
    assert unknown.startswith('Click "the element div > a:nth-of-type(7)"')


def test_the_approval_card_of_the_real_registry_uses_that_wording():
    store.get_or_create_singleton("browser")
    registry = CapabilityRegistry()
    capabilities.sync(registry)
    browser_connector._remember({"url": "https://example.com/", "elements": [
        {"selector": "body > a", "tag": "a", "label": "Learn more"}]})
    spec = registry.get("browser_click")
    assert spec.summarize({"selector": "body > a"}) == 'Click "Learn more" on example.com?'


def test_a_page_that_is_still_loading_is_said_to_be_loading_and_not_to_be_clicked_again():
    page = browser_connector._page({"url": "https://a.test/", "title": "Loading https://a.test/x",
                                    "text": "", "loading": True}, 1000)
    assert page["loading"] is True
    assert "still loading" in page["pageNote"] and "Do not repeat" in page["pageNote"]


def test_an_address_that_started_a_download_says_there_is_no_page_text():
    page = browser_connector._page({"url": "about:blank", "title": "", "text": "",
                                    "startedDownload": "https://a.test/x.pdf"}, 1000)
    assert page["startedDownload"] is True and "file download" in page["pageNote"]


@needs_browser
def test_a_slow_site_does_not_stall_the_agent_or_make_it_think_the_click_failed(site):
    """The whole path the model uses, against a link whose page takes twelve seconds."""
    import time

    site.handler.slow_seconds = 12
    dispatch = lambda name, **args: browser_connector.dispatch(name, args, {})  # noqa: E731
    opened = dispatch("browser_navigate", url=f"{BASE}/slow-link")
    link = next(e["selector"] for e in opened["elements"] if e["label"] == "Go to the slow page")
    started = time.time()
    clicked = dispatch("browser_click", selector=link)
    assert clicked["ok"] is True and clicked["loading"] is True
    assert "Do not repeat" in clicked["pageNote"]
    assert time.time() - started < 12
    deadline = time.time() + 20
    arrived = dispatch("browser_read_page")
    while arrived.get("loading") or "very-slow" not in arrived["url"]:
        assert time.time() < deadline, "the page never arrived"
        time.sleep(1)
        arrived = dispatch("browser_read_page")
    assert "the slow page arrived" in arrived["text"]


@needs_browser
def test_the_model_is_told_when_a_page_showed_a_dialog_and_how_it_was_answered(site):
    """A confirm is answered "No" and a prompt cancelled - nothing a page asks "are you sure?"
    about is agreed to for the person - but the model used to be told nothing, so it could
    neither explain why a delete did not happen nor ask the person about it."""
    dispatch = lambda name, **args: browser_connector.dispatch(name, args, {})  # noqa: E731
    page = dispatch("browser_navigate", url=f"{BASE}/dialogs")
    pick = lambda label: next(e["selector"] for e in page["elements"] if e["label"] == label)  # noqa: E731
    confirm = dispatch("browser_click", selector=pick("Confirm"))
    assert confirm["windowEvents"] == [
        'A confirm dialog said "Delete everything?" and I answered "No" (never agreed to for Boss).']
    assert "confirm: false" in confirm["text"]
    alert = dispatch("browser_click", selector=pick("Alert"))
    assert alert["windowEvents"] == ['An alert dialog said "Heads up" and I closed it.']
    assert "windowEvents" not in dispatch("browser_read_page"), "an event was told twice"


@needs_browser
def test_the_model_is_told_when_a_page_tried_to_open_a_popup(site):
    dispatch = lambda name, **args: browser_connector.dispatch(name, args, {})  # noqa: E731
    page = dispatch("browser_navigate", url=f"{BASE}/popup-page")
    button = next(e["selector"] for e in page["elements"] if "pop-up" in e["label"])
    after = dispatch("browser_click", selector=button)
    assert after["url"] == f"{BASE}/popup-page", "the pop-up replaced the page"
    assert len(after["windowEvents"]) == 1
    assert "tried to open a pop-up window" in after["windowEvents"][0]
    assert f"{BASE}/two" in after["windowEvents"][0]


@needs_browser
def test_a_page_with_content_in_a_frame_says_so_rather_than_looking_sparse(site):
    page = browser_connector.dispatch("browser_navigate", {"url": f"{BASE}/frames"}, {})
    assert page["embeddedFrames"] == [f"{BASE}/frame-inside"]
    assert "Take control" in page["frameNote"]
    plain = browser_connector.dispatch("browser_navigate", {"url": f"{BASE}/two"}, {})
    assert "embeddedFrames" not in plain
