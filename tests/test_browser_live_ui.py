"""The browser as a person meets it: the real chat screen, the real model, a real website.

Opt-in like `test_browser_live.py` (`JARVIS_LIVE_TESTS=1`), because it spends model quota. What it
proves is the hand-over that is the point of the feature: Jarvis opens a page, the chat shows the
window live, the person TAKES CONTROL in the console and moves it somewhere else, and when they
tell Jarvis to carry on, Jarvis sees where the person took it.
"""

from __future__ import annotations

import os
import time

import pytest
from live_harness import REAL_ENV, REAL_MODELS, LiveApp
from test_browser_e2e import until
from test_shell_e2e import CHROME, refresh, visit

from jarvis.browser.locate import find_browser

pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

from playwright.sync_api import Page, sync_playwright  # noqa: E402

pytestmark = [
    pytest.mark.skipif(os.environ.get("JARVIS_LIVE_TESTS") != "1",
                       reason="live tests spend model quota: set JARVIS_LIVE_TESTS=1"),
    pytest.mark.skipif(not (REAL_ENV.exists() and REAL_MODELS.exists()),
                       reason="needs the person's own .env and models.yaml to read"),
    pytest.mark.skipif(CHROME is None, reason="no Chromium for the interface test"),
    pytest.mark.skipif(find_browser() is None, reason="no Chrome or Edge to drive"),
]


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    app = LiveApp(tmp_path_factory.mktemp("liveui") / "data")
    app.start()
    yield app
    app.stop()


@pytest.fixture
def page(live):
    with sync_playwright() as play:
        browser = play.chromium.launch(executable_path=str(CHROME), args=["--no-sandbox"])
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        visit(page, live.base)
        yield page
        context.close()
        browser.close()


def send(page: Page, text: str) -> None:
    page.fill("[data-testid=composer-input]", text)
    page.press("[data-testid=composer-input]", "Enter")


def last_assistant_text(live: LiveApp) -> str:
    spoken = [s["text"] for s in live.steps() if s["role"] == "assistant" and s["text"].strip()]
    return spoken[-1].lower() if spoken else ""


def wait_for_answer(live: LiveApp, needle: str, seconds: float = 150) -> str:
    deadline = time.time() + seconds
    while time.time() < deadline:
        text = last_assistant_text(live)
        if needle in text:
            return text
        time.sleep(1)
    raise AssertionError(f"the answer never mentioned {needle!r}; last said: {last_assistant_text(live)!r}")


def test_the_person_takes_over_and_jarvis_sees_where_they_took_it(live, page):
    live.new_chat()
    visit(page, live.base)
    send(page, "Go to https://example.com and tell me the title of the page.")

    # The chat shows the window, live, with what Jarvis is looking at.
    page.wait_for_selector("[data-testid=browser-card][data-state=live]", timeout=150000)
    card = page.locator("[data-testid=browser-card]")
    assert "Example Domain" in card.locator("[data-testid=browser-card-title]").inner_text()
    assert card.locator("[data-testid=browser-card-site]").inner_text() == "example.com"
    assert page.evaluate("el => el.naturalWidth", card.locator(
        "[data-testid=browser-card-shot]").element_handle()) == 1280
    wait_for_answer(live, "example")

    # The person takes control and sends the window somewhere else, by hand.
    with page.context.expect_page() as opened:
        page.click("[data-testid=browser-card-take-control]")
    console = opened.value
    try:
        until(console, "document.querySelector('#status').className === 'live'", 60)
    except AssertionError:
        raise AssertionError(
            "the console never went live: status "
            f"{console.evaluate('document.querySelector(\"#status\").textContent')!r}, error "
            f"{console.evaluate('document.querySelector(\"#error\").textContent')!r}; window "
            f"{live.window()}") from None
    console.fill("#url", "https://en.wikipedia.org/wiki/Main_Page")
    console.click("#address .go")
    until(console, "document.querySelector('#where').textContent.startsWith('Wikipedia')", 90)
    assert console.locator("#error").inner_text() == ""

    # The chat's own card follows the window.
    page.wait_for_function(
        "document.querySelector('[data-testid=browser-card-title]').textContent.includes('Wikipedia')",
        timeout=30000)

    # And when the person says they are done, Jarvis reads the page they left it on.
    send(page, "I took over the browser and moved it. Read the page again and tell me which "
               "website it is showing now.")
    answer = wait_for_answer(live, "wikipedia")
    assert "wikipedia" in answer
    assert live.results_of("browser_read_page"), "it answered without looking"
    console.close()


def test_the_window_can_be_closed_from_the_card_and_opened_again_still_signed_in(live, page):
    live.new_chat()
    visit(page, live.base)
    send(page, "Go to https://httpbin.org/cookies/set?card=kept and tell me the cookies.")
    page.wait_for_selector("[data-testid=browser-card][data-state=live]", timeout=150000)
    wait_for_answer(live, "kept")
    page.click("[data-testid=browser-card-close]")
    try:
        page.wait_for_selector("[data-testid=browser-card][data-state=closed]", timeout=30000)
    except Exception:
        raise AssertionError(
            f"the card never showed closed. card: {page.locator('[data-testid=browser-card]').inner_text()!r}; "
            f"window: {live.window()}") from None
    assert page.locator("[data-testid=browser-card-take-control]").count() == 0
    page.click("[data-testid=browser-card-reopen]")
    page.wait_for_selector("[data-testid=browser-card][data-state=live]", timeout=60000)
    assert "kept" in live.read_window()["text"], "reopening lost what the window had"
    refresh(page)
    page.wait_for_selector("[data-testid=browser-card]", timeout=60000)
