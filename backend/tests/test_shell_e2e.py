"""The built front end, in a real browser, against the real app.

This is the check a typecheck and a build cannot make: that the interface
actually renders, actually calls its routes, and actually holds the shape it was
asked to hold. Everything here runs against the REAL FastAPI app on a real
socket on an unusual port, serving the REAL static export — the only stand-in is
the model on the far end of the wire, which speaks the genuine OpenAI format.

Four of these tests are structural rather than cosmetic. The interface has four
fixed anchors, and the rest of the design is free to change underneath them:

  1. the hamburger is at the top LEFT and reaches every section;
  2. the orb stays centred and NOTHING on the page moves or resizes it;
  3. the conversation floats OVER the right edge of the stage, reserving no
     column;
  4. the conversation and the composer are ONE panel, not two.

A redesign that quietly breaks one of those is exactly what these catch — a
screenshot review would not, and a component test cannot see a layout at all.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from jarvis.gateway import connections, registry

from stub_openai_server import StubModelServer

pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

from playwright.sync_api import Page, sync_playwright  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EXPORT = REPO_ROOT / "frontend" / "out" / "index.html"

#: The browser this environment ships, pinned by path. The Python package's own
#: expected build number and the installed one differ, and downloading a second
#: copy of Chromium to satisfy a version string is not worth it.
CHROME = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")) \
    / "chromium-1194" / "chrome-linux" / "chrome"

pytestmark = [
    pytest.mark.skipif(not EXPORT.is_file(),
                       reason="the front end has not been built (cd frontend && npm run build)"),
    pytest.mark.skipif(not CHROME.is_file(), reason="no Chromium in this environment"),
]


@pytest.fixture
def stub(scratch):
    """A model that answers, registered in the scratch data dir only."""
    server = StubModelServer()
    base_url = server.start()
    conn = connections.add_connection(adapter="openai-compatible", base_url=base_url,
                                      label="stub", provider="custom", kind="local",
                                      key_required=False)
    registry.add_model(connection_id=conn["id"], model="stub-model")
    yield server
    server.stop()


@pytest.fixture
def page(stub, live_server):
    with sync_playwright() as play:
        browser = play.chromium.launch(executable_path=str(CHROME), args=["--no-sandbox"])
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        page.goto(live_server, wait_until="networkidle")
        yield page
        context.close()
        browser.close()


def box(page: Page, selector: str) -> dict:
    found = page.locator(selector).bounding_box()
    assert found is not None, f"{selector} has no box — it is not laid out"
    return found


# --- it is actually there ------------------------------------------------------

def test_the_app_loads_and_the_orb_mounts(page):
    """A canvas with a size is the honest check: React rendering an element that
    WebGL never drew into would pass a snapshot test and show a blank stage."""
    canvas = box(page, "[data-testid=orb-canvas]")
    assert canvas["width"] > 200 and canvas["height"] > 200


def test_every_section_is_reachable_from_the_hamburger(page):
    from jarvis import store  # noqa: F401  (keeps the scratch import graph honest)

    menu = box(page, "[data-testid=menu]")
    # Anchor 1: top left. Not "somewhere in the header".
    assert menu["x"] < 120 and menu["y"] < 120

    page.click("[data-testid=menu]")
    page.wait_for_selector("[data-testid=drawer][data-open=true]")
    entries = page.locator("[data-testid=drawer] button[data-testid^=nav-]")
    assert entries.count() == 12

    page.click("[data-testid=nav-models]")
    page.wait_for_url("**#/models")
    assert page.locator("h1").inner_text() == "Model Settings"
    # And back out again, without a reload.
    page.click("[data-testid=menu]")
    page.click("[data-testid=nav-home]")
    page.wait_for_selector("[data-testid=orb-canvas]")


# --- it actually talks to the backend ------------------------------------------

def test_a_typed_message_streams_a_real_reply(page, stub):
    stub.says("The kettle is on.")

    page.fill("[data-testid=composer-input]", "what are you up to")
    page.press("[data-testid=composer-input]", "Enter")

    page.wait_for_selector("text=The kettle is on.", timeout=15_000)
    # The user's own words stayed on screen too — a transcript that shows only
    # the reply is a transcript that lost half the conversation.
    assert "what are you up to" in page.locator("[data-testid=transcript]").inner_text()
    # It really went over the wire to the model, not into a stub in the browser.
    assert stub.requests, "the backend never called the model"


def test_the_bell_reads_what_the_backend_stored(page):
    from jarvis import notifications

    notifications.add(kind="system", title="Something happened while you were out.")
    page.reload(wait_until="networkidle")
    assert page.locator("[data-testid=bell] + span, [data-testid=bell] ~ span").first.inner_text() == "1"


# --- it holds the shape it was asked to hold -----------------------------------

def test_the_conversation_and_the_composer_are_one_panel(page):
    """Anchor 4. Both bands must be INSIDE the one panel element — two adjacent
    cards would satisfy a screenshot and fail this."""
    panel = page.locator("[data-testid=conversation]")
    assert panel.count() == 1
    assert panel.locator("[data-testid=transcript]").count() == 1
    assert panel.locator("[data-testid=composer-input]").count() == 1


def test_the_conversation_floats_over_the_stage_and_reserves_no_column(page):
    """Anchor 3. The stage must still span the whole width underneath it."""
    stage = box(page, "[data-testid=stage]")
    rail = box(page, "[data-testid=conversation-rail]")
    viewport = page.viewport_size
    assert viewport is not None

    assert rail["x"] > stage["x"] + stage["width"] / 2, "the panel is not at the right"
    assert rail["x"] + rail["width"] < viewport["width"]
    # The stage is not squeezed into the space to the LEFT of the panel: it runs
    # underneath it, which is what "floats over" means.
    assert stage["x"] + stage["width"] > rail["x"] + rail["width"] / 2


def test_nothing_on_the_page_moves_or_resizes_the_orb(page, stub):
    """Anchor 2, and the one that has historically broken: a long reply, an
    attachment chip or an opening panel used to push the layout around."""
    before = box(page, "[data-testid=orb-canvas]")

    stub.says("A much longer answer. " * 60)
    page.fill("[data-testid=composer-input]", "say a lot")
    page.press("[data-testid=composer-input]", "Enter")
    page.wait_for_selector("text=A much longer answer.", timeout=15_000)
    page.click("[data-testid=settings]")
    page.wait_for_selector("[data-testid=settings-panel]")

    after = box(page, "[data-testid=orb-canvas]")
    assert after == before


def test_the_page_itself_never_grows_a_scrollbar(page, stub):
    """Everything long scrolls inside the panel that owns it."""
    stub.says("Another long answer. " * 80)
    page.fill("[data-testid=composer-input]", "again")
    page.press("[data-testid=composer-input]", "Enter")
    page.wait_for_selector("text=Another long answer.", timeout=15_000)

    grew = page.evaluate("document.documentElement.scrollHeight > window.innerHeight + 1")
    assert grew is False
