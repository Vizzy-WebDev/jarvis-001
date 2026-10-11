"""Jarvis's own browser, end to end in a real browser: the agent opens a page, the chat
shows the window live, the person takes it over, and what they do is what Jarvis sees next.

The built front end, against a real FastAPI on a scratch port, with a stub model
(`stub_provider_server.py`) that calls the browser tools the way a real model would, and a
real Chrome or Edge behind Jarvis's own browser - the website it visits is the loopback
fixture (`browser_site.py`), reached through the production destination checks.
"""

from __future__ import annotations

import json

import httpx
import pytest
from browser_site import BASE, FixtureSite
from stub_provider_server import StubProvider
from test_shell_e2e import CHROME, EXPORT, connect_and_select, refresh, visit

from jarvis.browser import set_host_for_tests
from jarvis.browser.locate import find_browser

pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

from playwright.sync_api import Page, sync_playwright  # noqa: E402

pytestmark = [
    pytest.mark.skipif(not EXPORT.is_file(), reason="the front end has not been built"),
    pytest.mark.skipif(CHROME is None, reason="no Chromium in this environment"),
    pytest.mark.skipif(find_browser() is None, reason="no Chrome or Edge for Jarvis to drive"),
]

AGENT_WINDOW = "0a7e5d11-6a72-4a76-8a1c-000000000001"


@pytest.fixture(autouse=True)
def _fresh_app(scratch):
    """The tool registry remembers which connector record each tool belongs to, and that
    record lives in the previous test's scratch database. Rebuilt per test, before the
    server (which asks `scratch` too) is started."""
    from jarvis import assembly
    from jarvis.db import reset_for_tests as reset_db

    reset_db()
    assembly.reset_for_tests()
    yield
    set_host_for_tests(None)
    assembly.reset_for_tests()
    reset_db()


@pytest.fixture
def page(live_server):
    with sync_playwright() as play:
        browser = play.chromium.launch(executable_path=str(CHROME), args=["--no-sandbox"])
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        visit(page, live_server)
        yield page
        context.close()
        browser.close()


@pytest.fixture
def website(tmp_path):
    fixture = FixtureSite()
    set_host_for_tests(fixture.host(tmp_path / "sessions"))
    yield fixture
    set_host_for_tests(None)
    fixture.close()


@pytest.fixture
def model(live_server, monkeypatch):
    monkeypatch.setattr(StubProvider, "_has_tool_result", lambda self, body: '"role": "tool"'
                        in json.dumps((body.get("messages") or [{}])[-1]))
    stub = StubProvider("openai-chat", reply="Done.")
    stub.start()
    connect_and_select(live_server, stub)
    yield stub
    stub.stop()


def send(page: Page, text: str) -> None:
    page.fill("[data-testid=composer-input]", text)
    page.press("[data-testid=composer-input]", "Enter")


def ask_to_open(page: Page, model: StubProvider, url: str) -> None:
    model.tool = ("browser_navigate", {"url": url})
    send(page, f"open {url}")
    page.wait_for_selector("[data-testid=browser-card]", timeout=60000)
    model.tool = None


def until(page: Page, expression: str, seconds: float = 30) -> None:
    """Wait for a condition in a page whose policy forbids `eval` (which `wait_for_function`
    uses) - by asking it from outside."""
    import time

    deadline = time.time() + seconds
    while time.time() < deadline:
        if page.evaluate(expression):
            return
        time.sleep(0.25)
    raise AssertionError(f"never became true: {expression}")


def read_window(base: str) -> dict:
    return httpx.get(f"{base}/api/browsers/{AGENT_WINDOW}/read", timeout=30).json()


def test_opening_a_page_shows_the_window_live_in_the_chat(model, website, page, live_server):
    ask_to_open(page, model, f"{BASE}/")
    card = page.locator("[data-testid=browser-card]")
    page.wait_for_selector("[data-testid=browser-card][data-state=live]", timeout=30000)
    assert page.locator("[data-testid=approval]").count() == 0   # opening a page asks nothing
    assert "Fixture Home" in card.locator("[data-testid=browser-card-title]").inner_text()
    assert card.locator("[data-testid=browser-card-site]").inner_text() == "site.test"
    assert card.locator("[data-testid=browser-card-status]").inner_text() == "Live"
    shot = card.locator("[data-testid=browser-card-shot]")
    assert page.evaluate("el => el.naturalWidth", shot.element_handle()) == 1280
    assert page.evaluate("el => el.src.startsWith('blob:')", shot.element_handle()), \
        "outside content must be drawn from a local copy, never linked"
    assert page.locator("[data-testid=file-tile]").count() == 0, "a window is not a file"


def test_the_card_survives_a_reload_and_still_shows_the_window(model, website, page, live_server):
    ask_to_open(page, model, f"{BASE}/two")
    page.wait_for_selector("[data-testid=browser-card][data-state=live]", timeout=30000)
    refresh(page)
    page.wait_for_selector("[data-testid=browser-card][data-state=live]", timeout=30000)
    assert page.locator("[data-testid=browser-card]").count() == 1
    assert "Second" in page.locator("[data-testid=browser-card-title]").inner_text()


def test_take_control_opens_a_console_the_person_can_act_in(model, website, page, live_server):
    ask_to_open(page, model, f"{BASE}/")
    page.wait_for_selector("[data-testid=browser-card][data-state=live]", timeout=30000)
    with page.context.expect_page() as opened:
        page.click("[data-testid=browser-card-take-control]")
    console = opened.value
    console.wait_for_load_state("load")
    assert console.url.endswith(f"/api/browsers/{AGENT_WINDOW}/console")
    console.wait_for_selector("#status.live", timeout=30000)
    assert console.evaluate("document.querySelector('#screen').naturalWidth") == 1280
    assert console.evaluate("document.querySelector('#where').textContent").startswith("Fixture Home")

    # The person drives it from the console...
    console.fill("#url", f"{BASE}/two")
    console.click("#address .go")
    until(console, "document.querySelector('#where').textContent.startsWith('Second')")
    # ...and what they did is what Jarvis reads next, with nothing else done.
    assert read_window(live_server)["title"] == "Second"

    # Scroll and key buttons go through the same door.
    console.click("#down")
    console.click("[data-key=Tab]")
    console.wait_for_selector("#status.live", timeout=30000)
    assert console.locator("#error").inner_text() == ""
    # Nothing from the page itself is ever put into the console as markup.
    assert console.evaluate("document.querySelectorAll('script').length") == 1
    console.close()


def test_a_window_can_be_closed_from_the_card_and_opened_again_still_there(
        model, website, page, live_server):
    ask_to_open(page, model, f"{BASE}/two")
    page.wait_for_selector("[data-testid=browser-card][data-state=live]", timeout=30000)
    page.click("[data-testid=browser-card-close]")
    page.wait_for_selector("[data-testid=browser-card][data-state=closed]", timeout=30000)
    assert page.locator("[data-testid=browser-card-take-control]").count() == 0
    page.click("[data-testid=browser-card-reopen]")
    page.wait_for_selector("[data-testid=browser-card][data-state=live]", timeout=30000)
    assert read_window(live_server)["title"] == "Second", "it reopened at the page it was on"


def test_clicking_asks_first_and_then_really_clicks_the_page(model, website, page, live_server):
    ask_to_open(page, model, f"{BASE}/")
    page.wait_for_selector("[data-testid=browser-card][data-state=live]", timeout=30000)

    model.tool = ("browser_click", {"selector": "#go"})
    send(page, "press the button")
    page.wait_for_selector("[data-testid=approval]", timeout=30000)
    assert "nothing yet" in read_window(live_server)["text"], "it clicked before being allowed"
    model.tool = None
    page.click("[data-testid=approve]")
    page.wait_for_function(
        "document.querySelectorAll('[data-testid=transcript] [data-role=assistant]').length >= 2"
        " || document.body.innerText.includes('Done.')", timeout=60000)
    assert "the button was clicked" in read_window(live_server)["text"]


def test_a_page_jarvis_may_not_open_says_so_in_the_chat_and_shows_no_window(
        model, website, page, live_server):
    model.tool = ("browser_navigate", {"url": "http://127.0.0.1:3000/api/health"})
    send(page, "please read what is at http://127.0.0.1:3000/api/health and tell me")
    page.wait_for_selector("[data-testid=transcript]")
    page.wait_for_function("document.body.innerText.includes('Done.')", timeout=60000)
    assert page.locator("[data-testid=browser-card]").count() == 0
    # What the model was actually told, read from what was stored - not from what it said.
    from jarvis.db import get_db

    stored = " ".join(str(row[0]) for row in get_db().execute(
        "SELECT payload FROM messages WHERE payload IS NOT NULL").fetchall())
    assert "BLOCKED_URL" in stored and "off limits" in stored
    assert "ui_action" not in stored, "a refused page must not leave a window card behind"


def bring_down_the_report(page: Page, model: StubProvider, live_server: str) -> None:
    """Open the downloads page, then click its link (which asks first, like any click)."""
    ask_to_open(page, model, f"{BASE}/downloads")
    page.wait_for_selector("[data-testid=browser-card][data-state=live]", timeout=30000)
    model.tool = ("browser_click", {"selector": "#pdf"})
    send(page, "download the report")
    page.wait_for_selector("[data-testid=approval]", timeout=30000)
    model.tool = None
    page.click("[data-testid=approve]")


def test_a_pdf_that_came_down_can_be_kept_from_the_card_and_lands_on_the_artifacts_page(
        model, website, page, live_server):
    bring_down_the_report(page, model, live_server)
    page.wait_for_selector("[data-testid=browser-card-pdf]", timeout=60000)
    assert "A PDF was downloaded." in page.locator("[data-testid=browser-card-pdf]").inner_text()
    page.click("[data-testid=browser-card-keep-pdf]")
    page.wait_for_selector("[data-testid=browser-card-kept]", timeout=30000)
    assert "report.pdf" in page.locator("[data-testid=browser-card-kept]").inner_text()
    assert page.locator("[data-testid=browser-card-pdf]").count() == 0, "it is no longer waiting"

    page.click("[data-testid=browser-card-kept] a")
    page.wait_for_selector("[data-testid=artifact-detail]", timeout=30000)
    assert "report" in page.inner_text("[data-testid=artifact-detail]").lower()


def test_the_agent_can_keep_a_pdf_and_it_appears_in_the_chat_as_a_file(
        model, website, page, live_server):
    bring_down_the_report(page, model, live_server)
    page.wait_for_selector("[data-testid=browser-card-pdf]", timeout=60000)
    model.tool = ("browser_save_downloads", {})
    send(page, "keep that report")
    page.wait_for_selector("[data-testid=file-tile][data-artifact-id]", timeout=60000)
    assert "report" in page.locator("[data-testid=file-tile]").first.inner_text().lower()
    refresh(page)
    page.wait_for_selector("[data-testid=file-tile][data-artifact-id]", timeout=30000)
