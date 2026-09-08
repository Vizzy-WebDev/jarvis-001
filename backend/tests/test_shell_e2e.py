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


# --- the composer's real shape -------------------------------------------------

def test_the_controls_sit_on_their_own_line_below_the_text(page):
    """Not a style preference. The original wraps them onto a permanent second
    line because sharing the textarea's line is what made a grown message clip
    the control row off — and a narrow panel leaves no room to type besides."""
    text = box(page, "[data-testid=composer-input]")
    controls = box(page, "[data-testid=composer-controls]")
    assert controls["y"] >= text["y"] + text["height"] - 1, "the controls are not below the text"

    attach = box(page, "[data-testid=attach]")
    send = box(page, "[data-testid=send]")
    # Attach at the left, send at the right, both on the SAME line.
    assert attach["x"] < send["x"]
    assert abs(attach["y"] - send["y"]) < 2


def test_attachments_scroll_sideways_and_never_stack(page, tmp_path):
    """Attaching a tenth file must not grow the composer downwards into the
    conversation. One row, scrolled horizontally — never a wrapping grid."""
    files = []
    for n in range(8):
        path = tmp_path / f"note-{n}.txt"
        path.write_text(f"attachment {n}", encoding="utf-8")
        files.append(str(path))
    page.set_input_files("input[type=file]", files)
    page.wait_for_selector("[data-testid=attachments]")
    page.wait_for_function(
        "() => document.querySelectorAll('[data-testid=attachments] > div').length === 8")

    row = page.locator("[data-testid=attachments]")
    tiles = page.locator("[data-testid=attachments] > div")
    tops = {round(tiles.nth(i).bounding_box()["y"]) for i in range(tiles.count())}
    assert len(tops) == 1, "the tiles wrapped onto more than one line"

    metrics = row.evaluate("el => ({scroll: el.scrollWidth, client: el.clientWidth})")
    assert metrics["scroll"] > metrics["client"], "the row is not actually scrollable sideways"

    # And the composer still did not push the orb around.
    assert page.evaluate("document.documentElement.scrollHeight <= window.innerHeight + 1")


# --- nothing is a static display ----------------------------------------------

def test_a_notification_opens_and_reading_it_marks_it_read(page):
    from jarvis import notifications

    notifications.add(kind="task_run", level="warning", title="The briefing didn't run.",
                      body="No model was available at 07:00.")
    page.goto(page.url.split("#")[0] + "#/notifications", wait_until="networkidle")

    page.wait_for_selector("[data-testid=notification-row]")
    page.click("[data-testid=notification-row]")
    page.wait_for_selector("[data-testid=modal]")
    assert "No model was available" in page.locator("[data-testid=modal]").inner_text()

    page.click("[data-testid=modal-close]")
    page.wait_for_selector("[data-testid=modal]", state="detached")
    # Reading it is what "read" means — and the backend was actually told.
    assert notifications.unread_count() == 0


def test_a_notification_can_be_deleted_from_its_own_detail(page):
    from jarvis import notifications

    notifications.add(kind="system", title="Something happened.")
    page.goto(page.url.split("#")[0] + "#/notifications", wait_until="networkidle")
    page.click("[data-testid=notification-row]")
    page.click("[data-testid=modal] >> text=Delete")
    page.wait_for_selector("[data-testid=notification-row]", state="detached")
    assert notifications.listed() == []


def test_a_task_can_be_created_edited_paused_and_deleted_from_the_screen(page):
    from jarvis.scheduler import task_store

    page.goto(page.url.split("#")[0] + "#/tasks", wait_until="networkidle")
    page.click("[data-testid=new-task]")
    page.fill("[data-testid=task-title]", "Morning summary")
    page.select_option("[data-testid=task-repeat]", "weekdays")
    page.fill("[data-testid=task-prompt]", "tell me what's due today")
    page.click("[data-testid=modal] >> text=Save")

    page.wait_for_selector("[data-testid=task-row]")
    [task] = task_store.list_tasks()
    assert task["title"] == "Morning summary"
    assert task["recurrence"]["type"] == "weekdays"
    # The schedule sentence shown under the title is the BACKEND's, not a second
    # implementation in the browser.
    assert "weekday" in page.locator("[data-testid=task-row]").inner_text().lower()

    # The switch works from the list, without opening anything.
    page.click("[data-testid=task-row] [role=switch]")
    page.wait_for_function("() => document.querySelector('[role=switch]')"
                           ".getAttribute('aria-checked') === 'false'")
    assert task_store.get_task(task["id"])["enabled"] is False
    assert task_store.get_task(task["id"])["nextRunAt"] is None

    page.click("[data-testid=task-open]")
    page.wait_for_selector("[data-testid=modal]")
    page.click("[data-testid=modal] >> text=Delete")
    page.wait_for_selector("[data-testid=task-row]", state="detached")
    assert task_store.list_tasks() == []


def test_an_approval_in_the_transcript_is_a_real_control(page, stub):
    """The one place a static display is not merely unhelpful but wrong: the run
    is stopped, waiting for this answer."""
    ran = []
    from jarvis.assembly import get_registry
    from jarvis.capabilities import CapabilitySpec, Risk

    get_registry().register(CapabilitySpec(
        id="builtin.send_the_email", name="send_the_email",
        description="send an email", input_schema={"type": "object", "properties": {}},
        risk=Risk.HIGH, handler=lambda **kw: ran.append(kw) or "sent"))

    stub.calls_tool("send_the_email", {})
    stub.says("Sent.")

    page.fill("[data-testid=composer-input]", "email the invoice")
    page.press("[data-testid=composer-input]", "Enter")

    page.wait_for_selector("[data-testid=approval]", timeout=15_000)
    assert not ran, "it acted before anyone said yes"

    page.click("[data-testid=approve]")
    page.wait_for_selector("[data-testid=approval] >> text=You allowed this", timeout=15_000)
    assert ran, "allowing it did not actually run anything"
