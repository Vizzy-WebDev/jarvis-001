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

import json
import os
from pathlib import Path

import pytest

from jarvis.model_system.providers import AuthMethod, ProviderKind, add_provider
from jarvis.model_system.registry import add_model, list_models

from stub_oauth_server import StubOAuthServer
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
    server.base_url = base_url = server.start()
    provider = add_provider(label="stub", kind=ProviderKind.LOCAL, adapter="openai_compatible",
                            base_url=base_url, auth_method=AuthMethod.NONE, key_required=False)
    add_model(provider_id=provider.id, native_model_id="stub-model")
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


def test_a_sent_image_shows_in_the_senders_own_bubble_and_expands_on_click(page, tmp_path, stub):
    """Two previously-real gaps, closed together: an attachment the user sent
    used to vanish from the transcript entirely once sent (their own bubble
    carried only text, never the file) — and nothing anywhere in the
    transcript was clickable to see a full-size view."""
    import base64

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    path = tmp_path / "photo.png"
    path.write_bytes(png)

    stub.says("Nice photo.")
    page.set_input_files("input[type=file]", [str(path)])
    page.wait_for_selector("[data-testid=attachments]")
    page.click("[data-testid=send]")

    page.wait_for_selector("text=Nice photo.", timeout=15_000)
    tile = page.locator("[data-testid=attachment-tile]").first
    tile.wait_for()
    img_src = tile.locator("img").get_attribute("src")
    assert img_src and "/api/uploads/" in img_src and img_src.endswith("/content")

    tile.click()
    page.wait_for_selector("[data-testid=attachment-lightbox]")
    lightbox_img = page.locator("[data-testid=attachment-lightbox] img")
    assert lightbox_img.get_attribute("src") == img_src

    page.keyboard.press("Escape")
    page.wait_for_selector("[data-testid=attachment-lightbox]", state="detached")


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


def test_the_unread_filter_actually_filters(page):
    from jarvis import notifications

    a = notifications.add(kind="system", title="Read already")
    notifications.add(kind="system", title="Still unread")
    notifications.mark_read(a["id"])

    page.goto(page.url.split("#")[0] + "#/notifications", wait_until="networkidle")
    assert page.locator("[data-testid=notification-row]").count() == 2

    page.click("[data-testid=notification-filter-unread]")
    rows = page.locator("[data-testid=notification-row]")
    assert rows.count() == 1
    assert "Still unread" in rows.inner_text()

    page.click("[data-testid=notification-filter-all]")
    assert page.locator("[data-testid=notification-row]").count() == 2


def test_clearing_sends_notifications_to_a_real_recycle_bin_and_back(page):
    """The whole point of the recycle bin: Clear must not be a hard delete —
    it has to be restorable, and only genuinely gone once emptied for good."""
    from jarvis import notifications

    notifications.add(kind="system", title="Something happened.")
    page.goto(page.url.split("#")[0] + "#/notifications", wait_until="networkidle")

    page.click("[data-testid=clear-all]")
    page.wait_for_selector("[data-testid=notification-row]", state="detached")
    assert notifications.listed() == []

    page.click("[data-testid=open-recycle-bin]")
    page.wait_for_selector("[data-testid=recycle-bin-row]")
    assert len(notifications.trash_listed()) == 1

    page.click("[data-testid=restore-notification]")
    page.wait_for_selector("[data-testid=recycle-bin-row]", state="detached")
    assert notifications.trash_listed() == []
    assert len(notifications.listed()) == 1


def test_emptying_the_recycle_bin_permanently_deletes(page):
    from jarvis import notifications

    notifications.add(kind="system", title="Something happened.")
    notifications.clear_all()

    page.goto(page.url.split("#")[0] + "#/notifications", wait_until="networkidle")
    page.click("[data-testid=open-recycle-bin]")
    page.wait_for_selector("[data-testid=recycle-bin-row]")

    page.click("[data-testid=empty-recycle-bin]")
    page.wait_for_selector("[data-testid=recycle-bin-row]", state="detached")
    assert notifications.trash_listed() == []


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


def test_a_task_built_through_the_screen_actually_runs(page, stub):
    """The check the first version of this screen did not have, and the reason
    it shipped broken: it wrote `action.prompt` where the engine reads
    `action["text"]`, so every task it created was accepted happily and then
    failed the moment it ran. Creating and editing a task proves nothing about
    whether it works."""
    from jarvis.scheduler import engine, task_store

    page.goto(page.url.split("#")[0] + "#/tasks", wait_until="networkidle")
    page.click("[data-testid=new-task]")
    page.fill("[data-testid=task-title]", "Daily nudge")
    page.fill("[data-testid=task-prompt]", "remind me to stretch")
    page.click("[data-testid=modal] >> text=Save")
    page.wait_for_selector("[data-testid=task-row]")

    stub.says("Time to stretch.")
    [task] = task_store.list_tasks()
    run = engine.run_task_now(task["id"])

    assert run["ok"] is True, run.get("error")
    assert "stretch" in run["summary"].lower()


def _connect(label: str, kind: str = "api") -> str:
    """A genuinely CONNECTED connector, not just an added one — `isPickable()`
    now requires `status.state == 'working'`, not just `enabled`, so a picker
    test has to simulate a real completed connection to mean what its own
    name says."""
    from jarvis.connectors import store

    connector = store.add_connector(type=kind, label=label)
    store.update_connector(connector["id"], {
        "status": {"state": "working", "checkedAt": None, "detail": None}})
    return connector["id"]


def test_the_connector_picker_is_a_picker_not_a_list_of_names(page):
    """Chips carrying each app's own mark, and a dropdown of real rows with real
    switches — the thing the owner asked for, checked as structure rather than
    admired in a screenshot."""
    _connect("Notion")
    _connect("Gmail")

    page.goto(page.url.split("#")[0] + "#/tasks", wait_until="networkidle")
    page.click("[data-testid=new-task]")
    page.wait_for_selector("[data-testid=add-connector]")

    page.click("[data-testid=add-connector]")
    page.wait_for_selector("[data-testid=popover]")
    rows = page.locator("[data-testid=popover] [data-testid^=connector-row-]")
    assert rows.count() == 2
    # Each row carries a real mark, not a bullet: Notion is one of the
    # hand-authored brand paths, so it renders as an svg.
    assert rows.first.locator("svg, img").count() >= 1
    assert rows.first.locator("[role=switch]").count() == 1

    rows.first.locator("[role=switch]").click()
    page.wait_for_selector("[data-testid=connector-chip]")
    assert page.locator("[data-testid=connector-chip]").count() == 1


def test_a_long_connector_list_is_capped_and_see_more_opens_the_rest(page):
    """The cap is not cosmetic: the original found live that an unbounded list
    runs off the bottom of the screen."""
    for name in ("Notion", "Gmail", "Slack", "GitHub", "Google Drive", "Linear", "Jira"):
        _connect(name)

    page.goto(page.url.split("#")[0] + "#/tasks", wait_until="networkidle")
    page.click("[data-testid=new-task]")
    page.click("[data-testid=add-connector]")
    page.wait_for_selector("[data-testid=popover]")

    assert page.locator("[data-testid=popover] [data-testid^=connector-row-]").count() == 5
    page.click("[data-testid=see-more]")
    page.wait_for_selector("[data-testid=connector-search]")
    assert page.locator("[data-testid=modal] [data-testid^=connector-row-]").count() == 7

    page.fill("[data-testid=connector-search]", "git")
    page.wait_for_function(
        "() => document.querySelectorAll('[data-testid=modal] [data-testid^=connector-row-]')"
        ".length === 1")


def test_escape_closes_only_the_list_and_leaves_the_editor_open(page):
    """The latent bug this found: every modal listened for Escape on the window,
    so closing the inner list took the half-filled task with it."""
    for name in ("Notion", "Gmail", "Slack", "GitHub", "Google Drive", "Linear"):
        _connect(name)

    page.goto(page.url.split("#")[0] + "#/tasks", wait_until="networkidle")
    page.click("[data-testid=new-task]")
    page.fill("[data-testid=task-title]", "Half written")
    page.click("[data-testid=add-connector]")
    page.click("[data-testid=see-more]")
    page.wait_for_selector("[data-testid=connector-search]")

    page.keyboard.press("Escape")
    page.wait_for_selector("[data-testid=connector-search]", state="detached")
    # The editor survived, with what was typed in it.
    assert page.input_value("[data-testid=task-title]") == "Half written"


def test_a_connector_chosen_here_is_what_the_task_saves(page):
    from jarvis.scheduler import task_store

    connector_id = _connect("Notion")

    page.goto(page.url.split("#")[0] + "#/tasks", wait_until="networkidle")
    page.click("[data-testid=new-task]")
    page.fill("[data-testid=task-prompt]", "tidy my notes")
    page.click("[data-testid=add-connector]")
    page.click(f"[data-testid=connector-row-{connector_id}] [role=switch]")
    page.keyboard.press("Escape")
    page.click("[data-testid=modal] >> text=Save")
    page.wait_for_selector("[data-testid=task-row]")

    [task] = task_store.list_tasks()
    assert task["action"]["connectors"] == [connector_id]


def test_the_model_picker_offers_auto_and_every_real_model(page, stub):
    """Auto is the default and always first. A pinned model is saved on the
    task, and the run history reports which model actually answered — which can
    differ, since the gateway treats a pin as an ordering."""
    from jarvis.scheduler import engine, task_store

    page.goto(page.url.split("#")[0] + "#/tasks", wait_until="networkidle")
    page.click("[data-testid=new-task]")
    page.wait_for_selector("[data-testid=task-model]")

    options = page.locator("[data-testid=task-model] option")
    assert options.count() >= 2, "the stub model never reached the picker"
    assert "Auto" in options.nth(0).inner_text()

    pinned = options.nth(1).get_attribute("value")
    page.select_option("[data-testid=task-model]", pinned)
    page.fill("[data-testid=task-prompt]", "say something")
    page.click("[data-testid=modal] >> text=Save")
    page.wait_for_selector("[data-testid=task-row]")

    [task] = task_store.list_tasks()
    assert task["action"]["modelId"] == pinned

    stub.says("Something.")
    run = engine.run_task_now(task["id"])
    assert run["ok"] is True and run["modelId"] == pinned


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


# --- the models screen ---------------------------------------------------------

def test_the_balance_dial_is_reachable_and_takes_effect_on_the_next_turn(page, stub):
    """It was not, before this. `balance` was stored, defaulted and read by the
    router on every turn, and no screen anywhere set it — so the one control
    over "what should Jarvis favour when it chooses for itself" existed only in
    a JSON file."""
    from jarvis import prefs

    page.goto(page.url.split("#")[0] + "#/models", wait_until="networkidle")
    page.wait_for_selector("[data-testid=balance]", timeout=15_000)
    assert page.input_value("[data-testid=balance]") == "balanced"

    page.select_option("[data-testid=balance]", "quality")
    page.wait_for_timeout(400)

    assert prefs.get_prefs()["balance"] == "quality"


def test_the_by_model_view_shows_what_a_model_actually_is(page, stub):
    """The crossing axis, made visible: this view groups by what a model IS,
    where the default view groups by whose key reaches it."""
    page.goto(page.url.split("#")[0] + "#/models", wait_until="networkidle")
    page.wait_for_selector("[data-testid=view-model]", timeout=15_000)

    page.click("[data-testid=view-model]")
    page.wait_for_selector("[data-testid=catalog-list]", timeout=15_000)

    assert page.locator("[data-testid=catalog-version]").count() >= 1
    page.locator("[data-testid=catalog-version] button").first.click()
    page.wait_for_selector("[data-testid=version-facts]", timeout=10_000)

    # Three states reach the browser. A capability nobody has established is
    # marked as unknown, never rendered as a flat no.
    assert "?" in page.inner_text("[data-testid=version-facts]")




def test_a_model_can_be_added_through_the_screen_and_then_answers(page, stub):
    """The front door: nothing works until a model is added, so this walks the
    real three steps — pick a provider, give the address, choose models — and
    then proves the thing that was added can actually hold a conversation."""
    stub.models = [{"id": "alpha"}, {"id": "beta"}]

    page.goto(page.url.split("#")[0] + "#/models", wait_until="networkidle")
    page.click("[data-testid=add-model]")
    page.click("[data-testid=provider-local]")
    page.fill("[data-testid=base-url]", stub.base_url)
    page.click("[data-testid=find-models]")

    page.wait_for_selector("[data-testid=found-alpha]", timeout=15_000)
    page.click("[data-testid=found-alpha]")
    # Adding ONE model asks the specific question — can this model produce a
    # token — so the stub needs something to answer with.
    stub.says("ready")
    page.click("[data-testid=add-models]")

    page.wait_for_selector("[data-testid=connection-list] >> text=alpha", timeout=15_000)
    # Beside the one the fixture already registered — "alpha" is the one this
    # test actually added, through the real screen.
    assert "alpha" in [m.native_model_id for m in list_models()]

    # And it is a real, usable model, not just a row: ask it something.
    stub.says("Hello from alpha.")
    page.goto(page.url.split("#")[0] + "#/", wait_until="networkidle")
    page.fill("[data-testid=composer-input]", "are you there")
    page.press("[data-testid=composer-input]", "Enter")
    page.wait_for_selector("text=Hello from alpha.", timeout=15_000)


def test_an_address_with_nothing_at_it_says_what_it_tried(page):
    """The failure this whole flow was rebuilt for: one generic sentence with no
    way to tell what went wrong."""
    page.goto(page.url.split("#")[0] + "#/models", wait_until="networkidle")
    page.click("[data-testid=add-model]")
    page.click("[data-testid=provider-custom]")
    page.fill("[data-testid=base-url]", "http://127.0.0.1:19999")
    page.click("[data-testid=find-models]")

    page.wait_for_selector("text=What Jarvis tried", timeout=20_000)
    assert page.locator("[data-testid=modal]").inner_text().strip()


def test_removing_a_connection_says_what_goes_with_it(page, stub):
    page.goto(page.url.split("#")[0] + "#/models", wait_until="networkidle")
    page.wait_for_selector("[data-testid=connection-card]")
    page.locator("[data-testid=connection-card]").first.get_by_text("Remove").click()

    # The count is stated before it happens, not discovered afterwards.
    assert "1 model" in page.locator("[data-testid=modal]").inner_text()
    page.click("[data-testid=confirm-remove]")
    page.wait_for_selector("[data-testid=connection-card]", state="detached")
    assert list_models() == []


def test_a_service_key_is_saved_and_never_shown_again(page):
    from jarvis import config

    page.goto(page.url.split("#")[0] + "#/models", wait_until="networkidle")
    page.click("[data-testid=add-service]")
    page.fill("[data-testid=service-label]", "Deepgram")
    page.fill("[data-testid=service-key]", "dg-secret-value-999")
    page.click("[data-testid=save-service]")

    page.wait_for_selector("[data-testid=service-list]")
    assert config.get_secret("deepgram") == "dg-secret-value-999"
    assert "dg-secret-value-999" not in page.content()


# --- the voice pickers ---------------------------------------------------------

def test_the_voice_pickers_offer_only_what_is_actually_available(page, stub):
    """Every option is computed from real state — a connected model's declared
    capabilities, a configured key — and an unavailable engine always arrives
    with a reason. "Not available" on its own is what people file bugs about
    when the fix was ten seconds away."""
    page.click("[data-testid=settings]")
    page.wait_for_selector("[data-testid=engine-options]")

    # A model is connected (the fixture's), so the two engines that only need a
    # model are live; the realtime one is not, because nothing declares it.
    assert page.is_enabled("[data-testid=engine-pipeline]")
    assert page.is_enabled("[data-testid=engine-duplex]")
    assert page.is_disabled("[data-testid=engine-realtime]")
    assert "realtime" in page.inner_text("[data-testid=engine-realtime]").lower()

    # The browser's own voice is always there: no key, no account, no server.
    assert page.locator("[data-testid=voice-browser]").count() == 1


def test_a_configured_voice_provider_appears_beside_the_browsers_own(page):
    from jarvis import external_services

    external_services.add_or_update(label="ElevenLabs", key="k")
    page.reload(wait_until="networkidle")
    page.click("[data-testid=settings]")
    page.wait_for_selector("[data-testid=voice-options]")

    assert page.locator("[data-testid=voice-browser]").count() == 1
    assert page.locator("[data-testid=voice-elevenlabs]").count() == 1


def test_the_mic_button_is_live_now(page):
    """It said "lands with the next wave" until this point. It takes a real
    microphone, which this browser is not given, so what is asserted is that it
    is offered at all and reports honestly when it cannot start."""
    assert page.is_enabled("[data-testid=mic]")
    page.click("[data-testid=mic]")
    # Either it took the mic or it said why not — never silence.
    page.wait_for_function(
        "() => document.querySelector('[data-testid=status]').textContent.trim().length > 0",
        timeout=10_000)


def test_mute_is_a_real_separate_control_disabled_with_no_session(page):
    """Muting used not to exist as its own control at all — the single mic
    button always did a full stop, interrupting Jarvis as a side effect.
    `setMuted()` was already correctly implemented on every engine (input
    capture only, never the current turn) but nothing in the UI ever called
    it. This proves the dedicated control actually exists and starts
    disabled, matching "nothing to mute yet" with no session running.

    What this deliberately does NOT attempt: driving it through a real
    speaking turn to prove muting never interrupts. This sandboxed Chromium
    cannot hold a stable `listening` state at all — its fake microphone
    doesn't satisfy the real Web Speech API, which errors out within
    milliseconds (confirmed directly, twice, while building this — see
    `test_pipeline_engine_stops_for_real_rather_than_being_silently_abandoned`
    above) — so `listening` itself is never stable long enough here to
    reliably reach `speaking`, let alone test muting during it, without
    writing a flaky test around a race this environment cannot resolve.
    `toggleMute` never calling `interrupt()`/`stop()` is verified by reading
    the code instead; a real microphone is the honest way to confirm the
    live behaviour, per the manual check in the project's own testing notes.
    """
    assert page.is_disabled("[data-testid=mute]")


# --- the engines that need a microphone ----------------------------------------

@pytest.fixture
def voice_page(stub, live_server):
    """A browser with a FAKE microphone, so an engine can genuinely start.

    Chromium's fake device is a real capture device as far as the page is
    concerned: `getUserMedia` resolves, an audio context runs, and frames flow.
    What it plays is a test tone, not speech, so nothing here can assert that a
    spoken sentence comes back — that needs a real microphone and a real
    provider, which is the owner's machine. What it CAN prove is everything up
    to that line: the engine takes the device, opens its socket, handles what
    the server answers, and still carries a typed turn end to end.
    """
    with sync_playwright() as play:
        browser = play.chromium.launch(executable_path=str(CHROME), args=[
            "--no-sandbox",
            "--use-fake-device-for-media-stream",
            "--use-fake-ui-for-media-stream",
        ])
        context = browser.new_context(viewport={"width": 1440, "height": 900},
                                      permissions=["microphone"])
        page = context.new_page()
        page.goto(live_server, wait_until="networkidle")
        yield page
        context.close()
        browser.close()


def start_engine(page: Page, engine: str) -> None:
    """Pick an engine in Settings, then start listening with it."""
    page.click("[data-testid=settings]")
    page.wait_for_selector("[data-testid=engine-options]")
    page.click(f"[data-testid=engine-{engine}]")
    page.click("[data-testid=settings]")  # close it again; it overlaps the stage
    page.click("[data-testid=mic]")


def test_the_engine_chosen_in_settings_is_the_one_that_runs(voice_page):
    """The picker was wired to nothing before this: whichever engine was chosen,
    the pipeline one started. Continuous listening opens a socket the pipeline
    engine never touches, so what the server is asked for is the proof."""
    opened: list[str] = []
    voice_page.on("websocket", lambda socket: opened.append(socket.url))

    start_engine(voice_page, "duplex")
    voice_page.wait_for_timeout(1500)

    assert any(url.endswith("/api/duplex") for url in opened), \
        f"the continuous-listening socket was never opened: {opened}"


def test_with_no_recognition_key_it_says_it_fell_back_rather_than_pretending(voice_page):
    """The server answers "use the browser's own" and closes. Silently behaving
    like a different engine, with none of the fast endpointing this one is
    chosen for, is not something anyone can be expected to work out."""
    start_engine(voice_page, "duplex")
    voice_page.wait_for_function(
        "() => (document.querySelector('[data-testid=status]')?.textContent || '')"
        ".toLowerCase().includes('browser')",
        timeout=15_000)


def test_a_typed_message_still_flows_while_that_engine_is_listening(voice_page, stub):
    """A typed turn goes through the running engine, not around it — the same
    path speech would take once there is speech to take it."""
    stub.says("Both hands are free.")
    start_engine(voice_page, "duplex")
    voice_page.wait_for_timeout(1000)

    voice_page.fill("[data-testid=composer-input]", "can you hear me")
    voice_page.press("[data-testid=composer-input]", "Enter")

    voice_page.wait_for_selector("text=Both hands are free.", timeout=15_000)
    assert stub.requests, "the backend never called the model"


def test_stopping_tears_the_engine_down_rather_than_leaving_it_open(voice_page):
    """An engine kept around "just in case" is a microphone kept open for no
    reason, and the browser shows a recording indicator the whole time.

    The socket closing is what is asserted, because it is genuinely observable
    from outside the page; whether the device track itself was released is not,
    so this proves teardown ran rather than claiming to prove the microphone is
    off. The two are the same call in `stop`.
    """
    closed: list[str] = []
    voice_page.on("websocket", lambda socket: socket.on("close", lambda _: closed.append(socket.url)))

    start_engine(voice_page, "duplex")
    voice_page.wait_for_timeout(1000)
    voice_page.click("[data-testid=mic]")  # off again
    voice_page.wait_for_timeout(500)

    assert any(url.endswith("/api/duplex") for url in closed), \
        "the continuous-listening socket was left open after stopping"


def test_pipeline_engine_stops_for_real_rather_than_being_silently_abandoned(voice_page):
    """A real, confirmed bug, found while testing the new quiet-window teardown
    below: on a recognition error, the UI used to just forget the engine (null
    the ref, flip the mic button off) without ever telling it to actually stop --
    so a live PipelineEngine kept running underneath, microphone still open,
    quietly retrying recognition forever, while the screen claimed otherwise.

    This fake-mic Chromium environment cannot drive a clean, error-free speech
    session at all -- `--use-fake-device-for-media-stream` audio doesn't satisfy
    the real Web Speech API, which errors immediately with `audio-capture` -- so
    rather than fighting that, this test uses it as the natural trigger and
    proves teardown is REAL: `MediaStreamTrack.stop()` is monkey-patched to count
    real calls (stronger than watching `aria-pressed` alone, which only proves
    the UI's belief, not the hardware release), a fresh click actually starts a
    new session rather than silently no-op-ing against an already-dead one, and
    the mic-blocked-style message is what's shown rather than being immediately
    overwritten by a generic one (see the same ordering fix in
    `onRecognitionError`).
    """
    voice_page.add_init_script(
        "window.__stoppedTracks = 0;"
        "const origStop = MediaStreamTrack.prototype.stop;"
        "MediaStreamTrack.prototype.stop = function () {"
        "  window.__stoppedTracks += 1;"
        "  return origStop.call(this);"
        "};"
    )
    voice_page.reload(wait_until="networkidle")  # the init script only applies from here

    start_engine(voice_page, "pipeline")
    voice_page.wait_for_selector("[data-testid=mic][aria-pressed=false]", timeout=10_000)
    assert "microphone" in voice_page.inner_text("[data-testid=status]").lower()
    assert voice_page.evaluate("window.__stoppedTracks") > 0, \
        "the microphone track was never actually released"

    # A fresh click must start a NEW session, not no-op against the dead one --
    # exactly the bug the page.tsx cleanup fix targets.
    voice_page.click("[data-testid=mic]")
    voice_page.wait_for_selector("[data-testid=mic][aria-pressed=true]", timeout=5_000)


def test_the_realtime_engine_is_offered_from_a_capability_and_fails_honestly(voice_page):
    """Two things at once, and both are the point.

    It is offered because a connected model's adapter DECLARES a realtime API —
    no provider is named anywhere in the picker, the socket, or the engine — and
    when the session cannot actually open, the start FAILS rather than leaving
    the microphone running under a screen claiming to listen. The original
    resolved on the socket merely opening, so a session that could never start
    took the microphone first and mentioned the problem afterwards.
    """
    provider = add_provider(label="realtime", kind=ProviderKind.NATIVE, adapter="gemini",
                            auth_method=AuthMethod.API_KEY, secret="not-a-real-key",
                            key_required=True)
    add_model(provider_id=provider.id, native_model_id="a-realtime-model")

    voice_page.reload(wait_until="networkidle")
    voice_page.click("[data-testid=settings]")
    voice_page.wait_for_selector("[data-testid=engine-realtime]:not([disabled])")

    opened: list[str] = []
    voice_page.on("websocket", lambda socket: opened.append(socket.url))
    voice_page.click("[data-testid=engine-realtime]")
    voice_page.click("[data-testid=settings]")
    voice_page.click("[data-testid=mic]")

    # The key is fake, so the session cannot open. What must happen is that it
    # says so and stops — never a silent microphone left running.
    voice_page.wait_for_selector("[data-testid=mic][aria-pressed=false]", timeout=25_000)
    assert any(url.endswith("/api/live") for url in opened), \
        f"the realtime socket was never opened: {opened}"
    assert voice_page.inner_text("[data-testid=status]").strip(), "it failed silently"


def test_the_composers_own_mic_is_live_and_stands_the_engine_down(voice_page):
    """Speaking INSTEAD of typing, which is a different thing from talking to
    Jarvis — it never sends and never reaches the server.

    Only one recognition session runs reliably at a time, so starting this has
    to stop whatever else is listening. That button said "lands with the voice
    engines" until now; they have landed.
    """
    assert voice_page.is_enabled("[data-testid=dictate]")

    start_engine(voice_page, "duplex")
    voice_page.wait_for_selector("[data-testid=mic][aria-pressed=true]")

    voice_page.click("[data-testid=dictate]")
    # The engine stood down rather than both holding the microphone at once.
    voice_page.wait_for_selector("[data-testid=mic][aria-pressed=false]", timeout=10_000)

    # Whether it can actually LISTEN depends on a speech service this browser
    # has no route to, so that is not asserted. What is: it either starts or
    # says why, and never simply switches itself off in silence.
    voice_page.wait_for_function(
        """() => {
             const button = document.querySelector('[data-testid=dictate]');
             const message = document.querySelector('[data-testid=composer-error]');
             return button?.getAttribute('aria-pressed') === 'true'
                 || (message?.textContent || '').trim().length > 0;
           }""",
        timeout=10_000)


# --- memory, and the profile notes that are one category of it ------------------

def go_to(page: Page, section: str) -> None:
    page.click("[data-testid=menu]")
    page.click(f"[data-testid=nav-{section}]")
    page.wait_for_url(f"**#/{section}")


def test_a_candidate_is_reviewed_on_screen_and_really_becomes_a_memory(page):
    """The review queue is the only part of this screen that is asking for
    something, so it is what gets checked first — and checked against the
    backend, not against what the screen says it did."""
    from jarvis.memory import store

    store.create_candidate(source_kind="chat", category="Work",
                           text="Works Tuesdays from home", confidence=0.7)

    go_to(page, "memory")
    page.wait_for_selector("[data-testid=review-queue]")
    page.click("[data-testid=approve-candidate]")
    page.wait_for_selector("[data-testid=memory-row]")

    saved = store.list_memories()
    assert [m["text"] for m in saved] == ["Works Tuesdays from home"]
    # Reviewed, not typed and not auto-saved: how consent was given is its own
    # fact, separate from where the content came from.
    assert saved[0]["origin"] == "approved"
    assert store.list_pending_candidates() == []


def test_rejecting_leaves_nothing_behind(page):
    from jarvis.memory import store

    store.create_candidate(source_kind="chat", category="Work",
                           text="Hates every Monday", confidence=0.3)
    go_to(page, "memory")
    page.click("[data-testid=reject-candidate]")
    page.wait_for_selector("[data-testid=review-queue]", state="detached")

    assert store.list_memories() == []
    assert store.list_pending_candidates() == []


def test_a_contradicted_memory_says_it_is_not_being_used(page):
    """The other half of "a conflict always needs a person": while it waits, the
    older memory stops being asserted to the model, and showing the row as though
    nothing were wrong would be saying something untrue."""
    from jarvis.memory import store

    existing = store.create_memory(category="Preferences", text="Uses a Mac",
                                   origin="explicit")
    store.create_candidate(source_kind="chat", category="Preferences",
                           text="Uses Windows now", confidence=0.9,
                           conflict_with=existing["id"])

    go_to(page, "memory")
    page.wait_for_selector("[data-testid=conflicted-flag]")

    # Resolving replaces rather than appends: two memories asserting opposite
    # things is the state this exists to prevent.
    page.click("[data-testid=use-new]")
    page.wait_for_selector("[data-testid=conflicted-flag]", state="detached")
    assert [m["text"] for m in store.list_memories()] == ["Uses Windows now"]


def test_editing_a_memory_keeps_what_it_used_to_say(page):
    from jarvis.memory import store

    memory = store.create_memory(category="Work", text="Works at Acme", origin="explicit")

    go_to(page, "memory")
    page.click("[data-testid=memory-row]")
    page.fill("[data-testid=memory-text]", "Works at Acme Corp")
    page.click("[data-testid=save-memory]")
    page.wait_for_selector("text=Works at Acme Corp")

    assert store.get_memory(memory["id"])["text"] == "Works at Acme Corp"
    history = store.version_history(memory["id"])
    assert [v["text"] for v in history] == ["Works at Acme", "Works at Acme"]


def test_archiving_then_deleting_is_two_steps_and_a_hard_delete_is_not_offered(page):
    """A change log's undo needs the row it points at to still exist, so a hard
    delete always goes through archive rather than being reachable from the live
    list."""
    from jarvis.memory import store

    memory = store.create_memory(category="Work", text="Sits by the window",
                                 origin="explicit")

    go_to(page, "memory")
    page.click("[data-testid=memory-row]")
    assert page.locator("[data-testid=delete-memory]").count() == 0  # not from a live row
    page.click("[data-testid=archive-memory]")
    page.wait_for_selector("[data-testid=memory-row]", state="detached")
    assert store.get_memory(memory["id"])["archived"] is True

    page.click("[data-testid=toggle-archived]")
    page.click("[data-testid=memory-row]")
    page.click("[data-testid=delete-memory]")
    page.wait_for_selector("[data-testid=memory-row]", state="detached")
    assert store.get_memory(memory["id"]) is None


def test_merging_duplicates_keeps_one_and_archives_the_other(page):
    """Archived, not deleted: a merge that turns out to be wrong is recoverable."""
    from jarvis.memory import store

    store.create_memory(category="About You", text="Has a dog", origin="explicit")
    store.create_memory(category="About You", text="Owns a dog called Rex",
                        origin="explicit")

    go_to(page, "memory")
    page.wait_for_selector("[data-testid=memory-row]")
    for index in range(2):
        page.locator("[data-testid=memory-select]").nth(index).check()
    page.click("[data-testid=merge-start]")
    page.fill("[data-testid=merge-text]", "Has a dog called Rex")
    page.click("[data-testid=merge-confirm]")
    page.wait_for_function(
        "() => document.querySelectorAll('[data-testid=memory-row]').length === 1")

    assert [m["text"] for m in store.list_memories()] == ["Has a dog called Rex"]
    assert len(store.list_memories(include_archived=True)) == 2


def test_the_trust_dial_really_changes_the_setting(page):
    """It lives on this screen and not in Settings: once a save can happen
    without being asked, seeing and undoing it stops being optional."""
    from jarvis.prefs import get_prefs

    go_to(page, "memory")
    page.wait_for_selector("[data-testid=trust-dial]")
    assert get_prefs()["memoryTrust"] == "ask"  # the safe default, untouched

    page.click("[data-testid=trust-balanced]")
    page.wait_for_selector("[data-testid=trust-balanced][aria-pressed=true]")
    page.wait_for_function("() => true")
    assert get_prefs()["memoryTrust"] == "balanced"


def test_a_profile_note_is_added_edited_and_read_back(page):
    from jarvis.memory import store

    go_to(page, "profile")
    page.fill("[data-testid=note-input]", "Shipping the new site by March")
    page.click("[data-testid=note-add]")
    page.wait_for_selector("[data-testid=note-row]")

    # The same rows as Memory, in one category — not a second store.
    assert [m["text"] for m in store.list_memories("About You")] \
        == ["Shipping the new site by March"]

    page.click("[data-testid=note-row]")
    page.fill("[data-testid=note-text]", "Shipping the new site by April")
    page.click("[data-testid=note-save]")
    page.wait_for_selector("text=Shipping the new site by April")

    note = store.list_memories("About You")[0]
    assert [v["text"] for v in store.version_history(note["id"])][0] \
        == "Shipping the new site by March"


def test_notes_read_in_the_order_they_were_written(page):
    """The browse view sorts newest first, which is right there and wrong here:
    a list of goals reads as a story, not as a feed."""
    go_to(page, "profile")
    for text in ("first thing", "second thing", "third thing"):
        page.fill("[data-testid=note-input]", text)
        page.click("[data-testid=note-add]")
        page.wait_for_selector(f"text={text}")

    rows = page.locator("[data-testid=note-row]").all_inner_texts()
    assert [row.split("\n")[0] for row in rows] \
        == ["first thing", "second thing", "third thing"]


# --- self-improvement, jobs, the briefing, and what is being watched for --------

def test_a_suggestion_is_approved_on_screen_and_the_rule_really_goes_live(page):
    """A single failure never becomes a rule — the pipeline enforces that — so
    this seeds a proposal that has already earned its place and checks the screen
    can actually apply it."""
    from jarvis.improvement import store

    store.create_proposal(kind="rule", title="Say less on the first pass",
                          rationale="It over-explains before anyone has asked",
                          payload={"text": "Say less on the first pass", "scope": "general"},
                          source_tier=1)

    go_to(page, "improvement")
    page.click("[data-testid=proposal-row]")
    page.click("[data-testid=approve-proposal]")
    page.wait_for_selector("[data-testid=proposal-row]", state="detached")

    assert [r["text"] for r in store.list_rules()] == ["Say less on the first pass"]


def test_undoing_that_change_really_takes_the_rule_back(page):
    from jarvis.improvement import store

    store.create_proposal(kind="rule", title="Lead with the answer",
                          payload={"text": "Lead with the answer", "scope": "general"},
                          source_tier=1)
    go_to(page, "improvement")
    page.click("[data-testid=proposal-row]")
    page.click("[data-testid=approve-proposal]")
    page.wait_for_selector("[data-testid=proposal-row]", state="detached")

    page.click("[data-testid=tab-changes]")
    page.click("[data-testid=undo-change]")
    page.wait_for_selector("[data-testid=change-row]:nth-child(2)")  # the undo is itself a row
    assert store.list_rules() == []


def test_an_undo_that_would_overwrite_a_later_decision_asks_first(page):
    """A refusal is a real answer to a real question, not a failure — so the
    screen shows a confirm rather than an error, and nothing has changed until
    the second click."""
    from jarvis.improvement import store

    store.create_proposal(kind="rule", title="Ask before assuming",
                          payload={"text": "Ask before assuming", "scope": "general"},
                          source_tier=1)
    go_to(page, "improvement")
    page.click("[data-testid=proposal-row]")
    page.click("[data-testid=approve-proposal]")
    page.wait_for_selector("[data-testid=proposal-row]", state="detached")

    # Muted by hand since. Undoing blind would silently overwrite that.
    rule = store.list_rules()[0]
    store.set_rule_active(rule["id"], False)

    page.click("[data-testid=tab-changes]")
    page.click("[data-testid=undo-change]")
    page.wait_for_selector("[data-testid=undo-force]")
    assert store.get_rule(rule["id"]) is not None  # nothing has happened yet

    page.click("[data-testid=undo-force]")
    page.wait_for_selector("[data-testid=undo-force]", state="detached")
    assert store.list_rules() == []


def test_an_idea_that_needs_code_offers_a_brief_and_never_an_apply_button(page):
    """Jarvis never edits its own code, so there is no apply here at all —
    generating the brief IS the approval for this kind."""
    from jarvis.improvement import store

    store.create_proposal(kind="code", title="Split the router in two",
                          rationale="It is doing two jobs", payload={"text": "..."},
                          source_tier=1)

    go_to(page, "improvement")
    page.click("[data-testid=proposal-row]")
    assert page.locator("[data-testid=approve-proposal]").count() == 0
    page.fill("[data-testid=brief-target]", "my coding assistant")
    page.click("[data-testid=generate-brief]")
    page.wait_for_selector("[data-testid=brief-text]")

    assert "Split the router in two" in page.input_value("[data-testid=brief-text]")
    assert store.list_rules() == []  # nothing was applied


def test_the_budgets_are_shown_rather_than_left_a_mystery(page):
    from jarvis.improvement import store

    go_to(page, "improvement")
    page.click("[data-testid=tab-settings]")
    shown = page.inner_text("[data-testid=improvement-budgets]")
    assert str(store.DAILY_BUDGET) in shown and str(store.WEEKLY_BUDGET) in shown


def test_a_job_is_read_beside_what_it_actually_did(page):
    """The trace is the point of this screen: it is how "it says it did this" is
    told apart from "it did this"."""
    from jarvis.jobs import job_store

    job = job_store.create_job(title="Read the archive", goal="find every mention")
    job_store.append_trace(job["id"], phase="intent", effect="read", kind="tool",
                           summary="reading the index page")
    job_store.append_trace(job["id"], phase="outcome", effect="read", kind="tool",
                           summary="found 12 matches")

    go_to(page, "jobs")
    page.click("[data-testid=job-row]")
    page.wait_for_selector("[data-testid=job-trace]")

    rows = page.locator("[data-testid=trace-row]").all_inner_texts()
    assert "reading the index page" in rows[0]
    assert "found 12 matches" in rows[1]


def test_desktop_work_shows_as_waiting_rather_than_claiming_to_run(page):
    """It is created already parked and the ordinary "keep going" is the only
    thing that starts it — so the screen has to say that, not show it as
    running."""
    from jarvis.jobs import job_store

    job = job_store.create_job(title="Tidy the desktop", goal="close everything",
                               kind="computer", status="awaiting_decision")
    job_store.add_outbox(tier=1, job_id=job["id"], reason="permission",
                         summary='"Tidy the desktop" would take over the computer. OK to start?')

    go_to(page, "jobs")
    assert "Waiting on you" in page.inner_text("[data-testid=job-status]")
    page.click("[data-testid=job-row]")
    page.wait_for_selector("[data-testid=job-waiting]")
    assert "take over the computer" in page.inner_text("[data-testid=job-waiting]")
    assert page.locator("[data-testid=job-resume]").count() == 1
    assert job_store.get_job(job["id"])["startedAt"] is None  # still not started


def test_a_job_whose_trace_reached_outside_is_not_offered_a_restart(page):
    """Repeating something that already left the machine is not something a
    retry can take back, and the record decides that, not the screen."""
    from jarvis.jobs import job_store

    job = job_store.create_job(title="Send the email", goal="send it")
    job_store.update_job(job["id"], {"recovery": "unrecoverable"})

    go_to(page, "jobs")
    page.click("[data-testid=job-row]")
    page.wait_for_selector("[data-testid=job-resume]")
    assert page.locator("[data-testid=job-restart]").count() == 0


def test_briefing_settings_save_and_are_really_stored(page):
    from jarvis.scheduler import briefing_config

    go_to(page, "briefing")
    page.wait_for_selector("[data-testid=briefing-sections]")
    assert briefing_config.get_config()["sections"]["tasks"] is True

    page.click("[data-testid=briefing-tasks]")
    page.wait_for_function(
        "() => document.querySelector('[data-testid=briefing-tasks]')"
        ".getAttribute('aria-checked') === 'false'")
    assert briefing_config.get_config()["sections"]["tasks"] is False
    # Merged, not replaced: the other sections survived a one-key save.
    assert briefing_config.get_config()["sections"]["greeting"] is True


def test_weather_and_headlines_are_shown_but_not_offered_as_things_to_add(page):
    """They are abilities Jarvis already has, not sources to attach. An earlier
    generation of this screen offered them through an "add a source" picker as
    though its own abilities were installable skills."""
    go_to(page, "briefing")
    weather = page.locator("[data-testid=briefing-weather]")
    page.wait_for_selector("[data-testid=briefing-weather]")
    assert "set my weather" in weather.inner_text()
    # Nothing to click: no switch, no field, no add button.
    assert weather.locator("button, input, select").count() == 0
    assert page.locator("[data-testid=briefing-headlines]").locator("button, input").count() == 0


def test_a_briefing_is_composed_for_real_and_says_which_model_wrote_it(page, stub):
    stub.says("Good morning. Nothing is scheduled today.")

    go_to(page, "briefing")
    page.click("[data-testid=briefing-preview]")
    page.wait_for_selector("text=Good morning. Nothing is scheduled today.", timeout=20_000)
    assert "Written by" in page.inner_text("[data-testid=briefing-result]")
    assert stub.requests, "the backend never called a model"


def test_a_watch_shows_in_the_shell_and_stopping_it_reaches_every_tab(page):
    """A click here and a spoken "stop watching that" must never leave two
    windows disagreeing, which is why this broadcasts. Checked in a SECOND tab,
    because a bar that clears in the tab that clicked proves only the click."""
    from jarvis.monitor import store as monitor_store

    monitor_store.create_monitor(description="the report landing in Downloads",
                                 check={"type": "file_exists", "path": "C:/x.pdf"},
                                 on_trigger={"type": "notify", "text": "it landed"})

    page.reload(wait_until="networkidle")
    page.wait_for_selector("[data-testid=watching-bar]")

    second = page.context.new_page()
    second.goto(page.url, wait_until="networkidle")
    second.wait_for_selector("[data-testid=watching-bar]")

    # Stopped from the Scheduled Tasks screen, in the first tab.
    go_to(page, "tasks")
    page.click("[data-testid=monitor-stop]")
    page.wait_for_selector("[data-testid=monitor-row]", state="detached")

    # The other tab found out without being touched.
    second.wait_for_selector("[data-testid=watching-bar]", state="detached", timeout=15_000)
    second.close()


# --- skills, chat history, and being taken somewhere by asking ------------------

def test_a_skill_is_written_here_and_really_lands_on_disk(page):
    go_to(page, "skills")
    page.click("[data-testid=add-skill]")
    page.click("[data-testid=add-way-write]")
    page.fill("[data-testid=skill-name]", "weekly-report")
    page.fill("[data-testid=skill-description]", "How to write the Friday report")
    page.fill("[data-testid=skill-instructions]", "Open with the headline number.")
    page.click("[data-testid=save-skill]")
    page.wait_for_selector("[data-testid=skill-row]")

    from jarvis.skills import files

    saved = files.list_user_skills()
    assert [s["name"] for s in saved] == ["weekly-report"]
    assert files.read_skill_md("weekly-report")["body"].strip() \
        == "Open with the headline number."


def test_nothing_jarvis_can_already_do_is_ever_listed_as_a_skill(page):
    """The rule this screen exists under, checked against the real thing: the
    built-in abilities are loaded and numerous, and none of them may appear
    here. It reads the folder list, which has no code path back to a
    capability."""
    from jarvis.assembly import get_registry
    from jarvis.capabilities import CapabilityKind

    built_in = {spec.name for spec in get_registry().list()
                if spec.kind is not CapabilityKind.SKILL}
    assert len(built_in) > 20, "the built-in tools did not load, so this proves nothing"

    go_to(page, "skills")
    page.wait_for_selector("[data-testid=add-skill]")
    shown = page.locator("[data-testid=skill-list]").inner_text() \
        if page.locator("[data-testid=skill-list]").count() else ""
    assert not (built_in & set(shown.split())), f"a built-in ability was listed: {shown}"


def test_a_skill_is_edited_and_turned_off_from_the_screen(page):
    from jarvis.skills import files

    files.create_skill(name="house-style", description="How we write",
                       instructions="Short sentences.", reserved=set())

    go_to(page, "skills")
    page.click("[data-testid=skill-open]")
    page.fill("[data-testid=detail-instructions]", "Short sentences. No jargon.")
    page.click("[data-testid=save-skill-edit]")
    page.wait_for_selector("[data-testid=skill-row]")
    assert "No jargon" in files.read_skill_md("house-style")["body"]

    page.click("[data-testid=skill-toggle]")
    page.wait_for_selector("[data-testid=skill-toggle][aria-checked=false]")
    assert files.get_skill("house-style")["enabled"] is False


def test_a_skill_is_deleted_from_its_own_detail(page):
    from jarvis.skills import files

    files.create_skill(name="temporary", description="x", instructions="y", reserved=set())
    go_to(page, "skills")
    page.click("[data-testid=skill-open]")
    page.click("[data-testid=delete-skill]")
    page.wait_for_selector("[data-testid=skill-row]", state="detached")
    assert files.list_user_skills() == []


def test_the_add_button_opens_a_menu_before_the_create_widget(page):
    """Clicking Add shows a choice of ways in first; the create/install widget
    itself only appears after one is picked."""
    go_to(page, "skills")
    page.click("[data-testid=add-skill]")
    page.wait_for_selector("[data-testid=add-way-write]")
    assert page.locator("[data-testid=add-way-write]").inner_text() == "Write skill instructions"
    assert page.locator("[data-testid=skill-name]").count() == 0  # not yet — menu first

    page.click("[data-testid=add-way-write]")
    page.wait_for_selector("[data-testid=skill-name]")
    assert page.locator("[data-testid=way-write][aria-pressed=true]").count() == 1


def test_create_with_jarvis_hands_the_composer_a_real_draft(page):
    """Picking it never opens the write/install widget — it drops a real,
    editable draft in the ordinary chat composer instead, per the historical
    'Create with Claude' behaviour this mirrors."""
    go_to(page, "skills")
    page.click("[data-testid=add-skill]")
    page.click("[data-testid=add-way-jarvis]")
    page.wait_for_url("**#/")
    page.wait_for_selector("[data-testid=composer-input]")
    typed = page.locator("[data-testid=composer-input]").input_value()
    assert "skill" in typed.lower() and len(typed) > 0
    assert page.locator("[data-testid=skill-name]").count() == 0  # no widget was opened


def test_the_skill_list_is_filtered_and_sorted_for_real(page):
    from jarvis.skills import files

    files.create_skill(name="zzz-last", description="z", instructions="z", reserved=set())
    files.create_skill(name="aaa-first", description="a", instructions="a", reserved=set())
    files.update_skill_state("aaa-first", {"enabled": False})

    go_to(page, "skills")
    page.wait_for_selector("[data-testid=skill-row]")

    page.click("[data-testid=skill-filter-off]")
    rows = page.locator("[data-testid=skill-row]")
    assert rows.count() == 1
    assert "aaa-first" in rows.inner_text()

    page.click("[data-testid=skill-filter-on]")
    rows = page.locator("[data-testid=skill-row]")
    assert rows.count() == 1
    assert "zzz-last" in rows.inner_text()

    page.click("[data-testid=skill-filter-all]")
    page.click("[data-testid=skill-sort-name]")
    names = page.locator("[data-testid=skill-row]").all_inner_texts()
    assert names[0].startswith("aaa-first")


def test_chat_history_searches_what_was_SAID_not_the_titles(page, stub):
    """Search runs on the server over the full transcript. Filtering titles in
    the browser would quietly answer a much worse question."""
    stub.says("The kettle is on.")
    page.fill("[data-testid=composer-input]", "put the kettle on")
    page.press("[data-testid=composer-input]", "Enter")
    page.wait_for_selector("text=The kettle is on.", timeout=15_000)

    go_to(page, "chat-history")
    page.wait_for_selector("[data-testid=history-row]")
    # The word appears nowhere in any title, only inside the conversation.
    page.fill("[data-testid=history-search]", "kettle")
    page.wait_for_function(
        "() => document.querySelectorAll('[data-testid=history-row]').length === 1")

    page.fill("[data-testid=history-search]", "zzz-nothing-said-this")
    page.wait_for_selector("[data-testid=history-row]", state="detached")


def test_opening_a_conversation_shows_what_was_said_without_resuming_it(page, stub):
    """Reading an old thread is the common thing and resuming it by accident is
    not, so they are two different actions."""
    # Deliberately not a question with a fast path: "what day is it" is answered
    # from the clock with no model call at all, so a scripted reply would never
    # be reached and this would fail for a reason that has nothing to do with
    # chat history.
    stub.says("Because it rained.")
    page.fill("[data-testid=composer-input]", "why did the picnic get cancelled")
    page.press("[data-testid=composer-input]", "Enter")
    page.wait_for_selector("text=Because it rained.", timeout=15_000)

    go_to(page, "chat-history")
    page.click("[data-testid=history-row]")
    # The container renders before the fetch resolves, so waiting for it alone
    # reads the empty state rather than the conversation.
    page.wait_for_selector("[data-testid=conversation-messages] >> text=Because it rained.",
                           timeout=10_000)
    body = page.inner_text("[data-testid=conversation-messages]")
    assert "why did the picnic get cancelled" in body and "Because it rained." in body
    # It is already the live one, so there is nothing to resume.
    assert page.locator("[data-testid=resume-conversation]").count() == 0


def test_the_chat_history_drawer_opens_from_the_hamburger_and_shows_pinned_first(page):
    from jarvis import chat_store

    alpha = chat_store.create_conversation()
    chat_store.rename_conversation(alpha["id"], "Alpha")
    beta = chat_store.create_conversation()
    chat_store.rename_conversation(beta["id"], "Beta")
    chat_store.set_pinned(beta["id"], True)

    page.click("[data-testid=chat-history-menu]")
    page.wait_for_selector("[data-testid=chat-history-drawer][data-open=true]")

    pinned_text = page.locator("[data-testid=drawer-pinned-list]").inner_text()
    assert "Beta" in pinned_text
    recent_text = page.locator("[data-testid=drawer-recent-list]").inner_text()
    assert "Alpha" in recent_text and "Beta" not in recent_text

    # The full-screen backdrop is what makes this a real overlay rather than
    # decoration — it intercepts clicks to whatever is behind it, the hamburger
    # itself included, so closing goes through Escape (or the backdrop/close
    # button), the same as the main hamburger Drawer.
    page.keyboard.press("Escape")
    page.wait_for_selector("[data-testid=chat-history-drawer][data-open=false]")


def test_the_drawer_slides_out_near_the_conversation_panel_not_the_left_edge(page):
    """A previously-real bug: this drawer's positioning was copy-pasted from
    the main, left-edge app Drawer, so it opened on the opposite side of the
    screen from its own trigger button — which lives inside the conversation
    panel, floating over the RIGHT edge of the stage."""
    page.click("[data-testid=chat-history-menu]")
    page.wait_for_selector("[data-testid=chat-history-drawer][data-open=true]")

    drawer_box = page.locator("[data-testid=chat-history-drawer]").bounding_box()
    rail_box = page.locator("[data-testid=conversation-rail]").bounding_box()
    viewport = page.viewport_size
    assert drawer_box and rail_box and viewport

    # On the right half of the screen, overlapping or directly adjacent to the
    # conversation panel — not flush against the left edge, where the main app
    # Drawer lives. Not exact-edge alignment: the two sit in different CSS
    # positioning contexts (fixed vs. absolute, different containing blocks),
    # so what matters is "near the panel," not pixel-identical edges.
    assert drawer_box["x"] > viewport["width"] / 2
    assert drawer_box["x"] <= rail_box["x"] + rail_box["width"], \
        "the drawer should open at or before the conversation panel's right edge"
    assert drawer_box["x"] >= rail_box["x"] - 60, \
        "the drawer should open near the conversation panel, not far from it"


def test_pinning_from_the_drawer_moves_it_into_the_pinned_group(page):
    from jarvis import chat_store

    convo = chat_store.create_conversation()
    chat_store.rename_conversation(convo["id"], "ToPin")

    page.click("[data-testid=chat-history-menu]")
    row = page.locator("[data-testid=drawer-chat-row]", has_text="ToPin")
    row.wait_for()
    assert page.locator("[data-testid=drawer-pinned-list]").count() == 0

    row.locator("[data-testid=drawer-toggle-pin]").click()
    page.wait_for_selector("[data-testid=drawer-pinned-list]")
    assert "ToPin" in page.locator("[data-testid=drawer-pinned-list]").inner_text()
    assert chat_store.get_conversation(convo["id"])["pinned"] is True


def test_clicking_new_chat_repeatedly_on_an_empty_conversation_does_not_duplicate_it(page):
    """A previously-real bug: reset_conversation() had no guard at all, so
    repeated clicks with nothing ever sent piled up an unbounded string of
    empty "New chat" rows."""
    from jarvis import chat_store

    before = len(chat_store.list_conversations())
    page.click("[data-testid=new-chat]")
    page.click("[data-testid=new-chat]")
    page.click("[data-testid=new-chat]")
    page.wait_for_timeout(300)

    assert len(chat_store.list_conversations()) == before


def test_the_archive_toggle_shows_only_archived_not_everything_mixed_in(page):
    """A previously-real bug: the toggle's "on" state ran an unfiltered query,
    mixing archived conversations into the SAME list as everything else
    rather than showing an isolated archived-only list."""
    from jarvis import chat_store

    visible = chat_store.create_conversation()
    chat_store.rename_conversation(visible["id"], "Visible One")
    hidden = chat_store.create_conversation()
    chat_store.rename_conversation(hidden["id"], "Archived One")
    chat_store.set_archived(hidden["id"], True)

    go_to(page, "chat-history")
    page.wait_for_selector("[data-testid=history-row]")
    body = page.inner_text("[data-testid=history-list]")
    assert "Visible One" in body and "Archived One" not in body

    page.click("[data-testid=toggle-archived]")
    page.wait_for_selector("text=Archived One")
    body = page.inner_text("[data-testid=history-list]")
    assert "Archived One" in body and "Visible One" not in body


def test_pinned_and_recent_sections_collapse_independently(page):
    from jarvis import chat_store

    pinned = chat_store.create_conversation()
    chat_store.rename_conversation(pinned["id"], "Pinned Thread")
    chat_store.set_pinned(pinned["id"], True)
    recent = chat_store.create_conversation()
    chat_store.rename_conversation(recent["id"], "Recent Thread")

    page.click("[data-testid=chat-history-menu]")
    page.wait_for_selector("[data-testid=drawer-pinned-list]")
    assert "Pinned Thread" in page.locator("[data-testid=drawer-pinned-list]").inner_text()
    assert "Recent Thread" in page.locator("[data-testid=drawer-recent-list]").inner_text()

    page.click("[data-testid=drawer-pinned-toggle]")
    assert page.get_attribute("[data-testid=drawer-pinned-toggle]", "aria-expanded") == "false"
    assert page.locator("[data-testid=drawer-pinned-list]").count() == 0
    # Collapsing Pinned must not touch Recent.
    assert "Recent Thread" in page.locator("[data-testid=drawer-recent-list]").inner_text()

    page.click("[data-testid=drawer-recent-toggle]")
    assert page.locator("[data-testid=drawer-recent-list]").count() == 0

    page.click("[data-testid=drawer-pinned-toggle]")
    page.wait_for_selector("[data-testid=drawer-pinned-list]")
    assert "Pinned Thread" in page.locator("[data-testid=drawer-pinned-list]").inner_text()


def test_view_all_in_the_drawer_opens_the_full_chat_history_page(page):
    page.click("[data-testid=chat-history-menu]")
    page.wait_for_selector("[data-testid=chat-history-drawer][data-open=true]")
    page.click("[data-testid=drawer-view-all]")
    page.wait_for_url("**#/chat-history")
    assert page.locator("h1").inner_text() == "Chat History"


def test_resuming_from_the_drawer_actually_loads_the_transcript(page):
    """A real, previously-live gap this closes: activating a conversation
    alone changes what the SERVER thinks is current, but does nothing to the
    panel's own `turns` state — so without a real reload, picking one up read
    as having silently done nothing."""
    from jarvis import chat_store

    convo = chat_store.create_conversation()
    chat_store.rename_conversation(convo["id"], "Old Thread")
    chat_store.append_message(convo["id"], {"role": "user", "text": "remember the rhubarb pie"})
    chat_store.append_message(convo["id"], {"role": "assistant", "text": "Noted: rhubarb pie."})

    page.click("[data-testid=chat-history-menu]")
    page.locator("[data-testid=drawer-resume]", has_text="Old Thread").click()
    page.wait_for_selector("[data-testid=chat-history-drawer][data-open=false]")
    page.wait_for_selector("text=Noted: rhubarb pie.", timeout=10_000)


def test_a_long_conversation_is_actually_scrollable(page):
    """A real, severe bug found empirically while testing the reported
    "can't scroll a long conversation" complaint — and worse than the
    original report suggested. `Transcript` used `justify-content: flex-end`
    on its own scroll container to keep a SHORT conversation sitting at the
    bottom. Once content overflows, Chromium never extends `scrollHeight`
    past `clientHeight` at all: the older messages render at a NEGATIVE
    `offsetTop`, genuinely unreachable by scrolling — confirmed on a
    completely FRESH page load with no resume, no picker, no prior
    navigation involved at all, so this was never specific to any one way of
    opening a conversation. Fixed by bottom-anchoring with a `margin-top:
    auto` spacer instead, which leaves the container's own scroll behaviour
    at its default (top-anchored, `scrollHeight` growing normally) — this
    proves the real content is both genuinely scrollable AND reachable, not
    just that a scrollbar exists.
    """
    from jarvis import chat_store, session

    convo = chat_store.create_conversation()
    for i in range(40):
        chat_store.append_message(convo["id"], {"role": "user", "text": f"message number {i}"})
    session.activate_conversation(convo["id"])

    page.goto(page.url, wait_until="networkidle")
    page.wait_for_selector("text=message number 39", timeout=10_000)

    scroll = page.locator("[data-testid=transcript]").evaluate(
        "el => ({ top: el.scrollTop, height: el.scrollHeight, client: el.clientHeight })")
    assert scroll["height"] > scroll["client"], \
        "a 40-message conversation should overflow a fixed-height panel"
    # Lands pinned to the bottom (the newest message), same as it always did.
    assert scroll["top"] + scroll["client"] >= scroll["height"] - 4

    # And the OLDEST message — the one that used to render off-screen at a
    # negative offset — must be genuinely reachable by scrolling to the top.
    page.locator("[data-testid=transcript]").evaluate("el => { el.scrollTop = 0; }")
    page.wait_for_selector("text=message number 0", timeout=5_000)


def test_resuming_a_conversation_from_the_drawer_lands_scrolled_to_the_bottom(page):
    """Resuming from this drawer updates `turns` on the SAME long-lived
    `Transcript` instance rather than remounting it, so its scroll-pin ref
    could stay stale `false` from having scrolled up in whatever conversation
    was open before — landing a freshly-resumed conversation wherever the
    LAST one happened to be scrolled, not at its own bottom. Keying
    `ConversationPanel` on the active conversation's id fixes that; this is
    independent of `test_a_long_conversation_is_actually_scrollable` above,
    which is the container actually being scrollable at all.
    """
    from jarvis import chat_store

    first = chat_store.create_conversation()
    chat_store.rename_conversation(first["id"], "First Thread")
    for i in range(40):
        chat_store.append_message(first["id"], {"role": "user", "text": f"message {i} in the first thread"})

    second = chat_store.create_conversation()
    chat_store.rename_conversation(second["id"], "Second Thread")
    for i in range(40):
        chat_store.append_message(second["id"], {"role": "assistant", "text": f"reply {i} in the second thread"})

    # Resume the first (long) conversation, then deliberately scroll away from
    # the bottom — this is what leaves the SAME Transcript instance's pin ref
    # stale `false` for whatever gets resumed next.
    page.click("[data-testid=chat-history-menu]")
    page.locator("[data-testid=drawer-resume]", has_text="First Thread").click()
    page.wait_for_selector("text=message 39 in the first thread", timeout=10_000)
    page.locator("[data-testid=transcript]").evaluate(
        "el => { el.scrollTop = 0; el.dispatchEvent(new Event('scroll')); }")

    # Now resume the second (also long) conversation from the same drawer.
    page.click("[data-testid=chat-history-menu]")
    page.locator("[data-testid=drawer-resume]", has_text="Second Thread").click()
    page.wait_for_selector("text=reply 39 in the second thread", timeout=10_000)

    scroll = page.locator("[data-testid=transcript]").evaluate(
        "el => ({ top: el.scrollTop, height: el.scrollHeight, client: el.clientHeight })")
    assert scroll["height"] > scroll["client"], "the transcript should be tall enough to scroll at all"
    assert scroll["top"] + scroll["client"] >= scroll["height"] - 4, \
        "the newly-resumed conversation should land scrolled to the bottom, not wherever the last one was left"


def test_deleting_a_chat_sends_it_to_a_real_recycle_bin_and_back(page):
    from jarvis import chat_store

    convo = chat_store.create_conversation()
    chat_store.rename_conversation(convo["id"], "Doomed")

    go_to(page, "chat-history")
    page.locator("[data-testid=history-row]", has_text="Doomed").click()
    page.click("[data-testid=delete-conversation]")
    page.wait_for_selector("[data-testid=modal]", state="detached")
    assert chat_store.is_trashed(convo["id"]) is True

    page.click("[data-testid=open-chat-recycle-bin]")
    page.locator("[data-testid=chat-recycle-bin-row]", has_text="Doomed").wait_for()

    page.locator("[data-testid=chat-recycle-bin-row]", has_text="Doomed") \
        .locator("[data-testid=restore-conversation]").click()
    page.wait_for_selector("[data-testid=chat-recycle-bin-row]", state="detached")
    assert chat_store.is_trashed(convo["id"]) is False


def test_permanently_deleting_from_the_chat_recycle_bin(page):
    from jarvis import chat_store

    convo = chat_store.create_conversation()
    chat_store.rename_conversation(convo["id"], "GoneForGood")
    chat_store.delete_conversation(convo["id"])

    go_to(page, "chat-history")
    page.click("[data-testid=open-chat-recycle-bin]")
    row = page.locator("[data-testid=chat-recycle-bin-row]", has_text="GoneForGood")
    row.wait_for()
    row.locator("[data-testid=delete-conversation-forever]").click()
    page.wait_for_selector("[data-testid=chat-recycle-bin-row]", state="detached")
    assert chat_store.is_conversation(convo["id"]) is False


def test_no_screen_draws_its_own_title_over_the_one_the_shell_draws(page):
    """A real bug this caught, and the reason it is now checked on every screen.

    The shell renders each section's title and blurb once, in the same place, for
    all of them — that sameness is most of what makes twelve screens feel like
    one product. Seven screens rendered a PageHeader of their own as well, so the
    title appeared twice and the column was padded twice. Nothing asserted on it
    until a Playwright strict-mode violation did, by accident.
    """
    from jarvis.tools.open_section import SECTIONS

    for section in sorted(SECTIONS):
        if section == "home":
            continue  # the assistant is the stage, not a titled screen
        page.evaluate(f"() => {{ window.location.hash = '#/{section}'; }}")
        page.wait_for_url(f"**#/{section}")
        count = page.locator("h1").count()
        assert count == 1, f"{section} draws {count} titles"


def test_asking_to_open_a_section_really_navigates(page, stub):
    """The tool returns WHERE to go and the browser goes there, through the same
    hash router the drawer drives — so a spoken request and a click land in
    exactly the same place."""
    stub.calls_tool("open_section", {"section": "memory"})
    stub.says("Opening Memory.")

    page.fill("[data-testid=composer-input]", "open my memory")
    page.press("[data-testid=composer-input]", "Enter")

    page.wait_for_url("**#/memory", timeout=20_000)
    assert page.locator("h1").inner_text() == "Memory"


# --- Connector: the connector screens, real OAuth, real tools -----------------

@pytest.fixture
def oauth_stub():
    server = StubOAuthServer()
    server.start()
    yield server
    server.stop()


def _open_app_control(page):
    page.evaluate("() => { window.location.hash = '#/app-control'; }")
    page.wait_for_url("**#/app-control")
    assert page.locator("h1").inner_text() == "Connector"


def test_a_custom_mcp_connector_connects_end_to_end_against_a_real_server(page, oauth_stub):
    """Add custom connector -> Connect -> a real popup carrying a real
    authorization code -> the real callback route redeems it -> the row shows
    Connected. Nothing here is mocked: the popup's own body is the stub
    server's real, PKCE-verified authorize response, and completing the flow
    is one real GET against `/api/connectors/oauth/callback`."""
    _open_app_control(page)

    page.click("[data-testid=add-connector-menu]")
    page.click("[data-testid=add-custom-connector]")
    page.fill("[data-testid=custom-label]", "Test Stub")
    page.fill("[data-testid=custom-mcp-url]", f"{oauth_stub.base_url}/mcp")
    page.click("[data-testid=save-custom-connector]")

    page.wait_for_selector("[data-testid=connect]")
    with page.expect_popup() as popup_info:
        page.click("[data-testid=connect]")
    popup = popup_info.value
    popup.wait_for_load_state()
    payload = json.loads(popup.inner_text("body"))
    popup.close()

    # What a real consent page's own redirect would have sent the browser to
    # — driven directly rather than through a fake browser-side redirect,
    # since the stub server (by design — see its own docstring) hands back
    # exactly what that redirect would have carried.
    callback = page.context.request.get(
        f"{live_server_url(page)}/api/connectors/oauth/callback",
        params={"code": payload["code"], "state": payload["state"], "iss": oauth_stub.base_url},
    )
    assert callback.ok

    # The connect card's own poll picks the change up on its own — no reload.
    page.wait_for_selector("[data-testid=modal]", state="detached", timeout=10_000)
    page.wait_for_selector("text=Connected")


def live_server_url(page: Page) -> str:
    return page.url.split("#", 1)[0].rstrip("/")


def test_toggling_a_connector_off_stops_its_tools_from_being_offered(page):
    """An `api` connector, not `mcp`: it can be given real operations
    directly (no live server to actually call), so this proves the
    underlying enable/disable mechanism (`connector_specs()`/
    `tool_names_for()`) rather than depending on a stub that speaks only
    enough MCP to probe OAuth, not to answer a real `tools/list`."""
    from jarvis.connectors import capabilities as connector_capabilities, store as connector_store

    _open_app_control(page)
    page.click("[data-testid=add-connector-menu]")
    page.click("[data-testid=add-custom-connector]")
    page.click("[data-testid=mechanism-api]")
    page.fill("[data-testid=custom-label]", "Toggle Me")
    page.fill("[data-testid=custom-base-url]", "https://api.example.invalid")
    page.click("[data-testid=save-custom-connector]")
    page.wait_for_selector("[data-testid=modal]")
    page.click("[data-testid=modal-close]")

    connector = next(c for c in connector_store.list_connectors(kind="api") if c["label"] == "Toggle Me")
    connector_store.update_connector(connector["id"], {"config": {"operations": [
        {"name": "test_op", "description": "A test operation.",
         "parameters": {"type": "object", "properties": {}}}]}})
    assert connector_capabilities.tool_names_for(connector["id"]) == ["toggle_me__test_op"]

    page.click("[data-testid=tab-api]")
    page.wait_for_selector("[data-testid=connector-row]")
    page.click("[data-testid=connector-row] [role=switch]")
    page.wait_for_function(
        "() => document.querySelector('[data-testid=connector-row] [role=switch]')"
        ".getAttribute('aria-checked') === 'false'",
    )
    assert connector_capabilities.tool_names_for(connector["id"]) == []


def test_an_unconnected_mcp_connector_shows_connect_not_a_toggle(page):
    """A row that looks the same whether or not sign-in ever finished is
    exactly what made an added-but-never-authorized connector indistinguishable
    from a real, working one. `mcp` specifically, since only it has a real
    Connect flow to hand the row a button for."""
    from jarvis.connectors import store as connector_store

    _open_app_control(page)
    page.click("[data-testid=add-connector-menu]")
    page.click("[data-testid=add-custom-connector]")
    page.fill("[data-testid=custom-label]", "Never Connected")
    page.fill("[data-testid=custom-mcp-url]", "https://mcp.example.invalid/mcp")
    page.click("[data-testid=save-custom-connector]")
    page.wait_for_selector("[data-testid=modal]")
    page.click("[data-testid=modal-close]")

    page.click("[data-testid=tab-mcp]")
    row = page.locator("[data-testid=connector-row]", has_text="Never Connected")
    row.wait_for()
    assert row.locator("[data-testid=connect-row]").count() == 1
    assert row.locator("[role=switch]").count() == 0
    assert "bg-ink-faint" in row.locator("[data-testid=connection-dot]").get_attribute("class")

    connector = next(c for c in connector_store.list_connectors(kind="mcp")
                     if c["label"] == "Never Connected")
    connector_store.update_connector(connector["id"], {
        "status": {"state": "working", "checkedAt": None, "detail": None}})
    page.reload(wait_until="networkidle")
    page.click("[data-testid=tab-mcp]")
    row = page.locator("[data-testid=connector-row]", has_text="Never Connected")
    row.wait_for()
    assert row.locator("[data-testid=connect-row]").count() == 0
    assert row.locator("[role=switch]").count() == 1
    assert "bg-state-ok" in row.locator("[data-testid=connection-dot]").get_attribute("class")


def test_the_group_permission_control_sets_every_tool_at_once_and_shows_custom(page):
    """The bulk category control and the per-tool three-button row, both real:
    setting the group changes every tool in it, and disagreeing tools read
    back as "Custom" rather than silently picking one."""
    from jarvis.connectors import store as connector_store

    _open_app_control(page)
    page.click("[data-testid=add-connector-menu]")
    page.click("[data-testid=add-custom-connector]")
    page.click("[data-testid=mechanism-api]")
    page.fill("[data-testid=custom-label]", "Grouped Ops")
    page.fill("[data-testid=custom-base-url]", "https://api.example.invalid")
    page.click("[data-testid=save-custom-connector]")
    page.wait_for_selector("[data-testid=modal]")

    connector = next(c for c in connector_store.list_connectors(kind="api")
                     if c["label"] == "Grouped Ops")
    connector_store.update_connector(connector["id"], {"config": {"operations": [
        {"name": "search_pets", "description": "Find pets.",
         "parameters": {"type": "object", "properties": {}}},
        {"name": "list_pets", "description": "List pets.",
         "parameters": {"type": "object", "properties": {}}},
    ]}})
    page.click("[data-testid=modal-close]")
    page.click("[data-testid=tab-api]")
    page.locator("[data-testid=connector-row]", has_text="Grouped Ops").click()
    page.wait_for_selector("[data-testid=group-permission]")

    # Both tools default to "allow" — the group control reads a real, single value.
    assert page.locator("[data-testid=group-permission]").input_value() == "allow"

    # Diverge one tool from the other; the group control must now say Custom.
    # The permission write is a real PATCH round trip, so wait for the button's
    # own state to flip before reading the (separately re-rendered) group
    # control — a plain assert right after the click would race the response.
    page.locator("[data-testid=tool-permission-ask]").first.click()
    page.wait_for_selector("[data-testid=tool-permission-ask][aria-pressed=true]")
    assert page.locator("[data-testid=group-permission]").input_value() == "custom"

    # Setting the group applies to every tool in it — both buttons agree again.
    # The handler awaits each tool's PATCH in sequence (never concurrently —
    # two requests racing a read-modify-write over the same connector record
    # could otherwise let the second clobber the first), so give both time.
    page.locator("[data-testid=group-permission]").select_option("deny")
    page.wait_for_function(
        "() => document.querySelectorAll("
        "'[data-testid=tool-permission-deny][aria-pressed=true]').length === 2",
        timeout=10_000,
    )

    # Stored (and looked up) under the PREFIXED name — the only name the
    # frontend, and therefore the permission it just saved, ever knows.
    updated = connector_store.get_connector(connector["id"])
    assert updated["config"]["toolPermissions"] == {
        "grouped_ops__search_pets": "deny", "grouped_ops__list_pets": "deny"}

    from jarvis.connectors import capabilities as connector_capabilities

    assert connector_capabilities.tool_names_for(connector["id"]) == []


def test_unconnected_apps_do_not_appear_in_the_connector_picker(page):
    """`isPickable()` requires a real `status.state === 'working'`, not just
    `enabled` — an added-but-never-authorized connector must not look like a
    usable app in the Scheduled Task editor's own picker."""
    from jarvis.connectors import store as connector_store

    connector_store.add_connector(type="mcp", label="Working App", enabled=True,
                                  config={"connectFlow": {"url": "https://mcp.example.invalid/mcp"}})
    working = next(c for c in connector_store.list_connectors(kind="mcp") if c["label"] == "Working App")
    connector_store.update_connector(working["id"], {
        "status": {"state": "working", "checkedAt": None, "detail": None}})
    connector_store.add_connector(type="mcp", label="Never Connected App", enabled=True,
                                  config={"connectFlow": {"url": "https://mcp2.example.invalid/mcp"}})

    page.goto(page.url.split("#")[0] + "#/tasks", wait_until="networkidle")
    page.click("[data-testid=new-task]")
    page.click("[data-testid=add-connector]")
    page.wait_for_selector("[data-testid=popover]")
    popover_text = page.locator("[data-testid=popover]").inner_text()
    assert "Working App" in popover_text
    assert "Never Connected App" not in popover_text


def test_the_catalogue_lists_official_connectors_with_a_real_resolved_icon(page, scratch):
    """Seeds the icon resolver's own cache file for Notion's real hostname —
    the honest way to verify the render path without a live external fetch —
    then asserts the Browse view's own <img> carries that exact data URI."""
    import time

    from jarvis.store import write_json

    seeded = "data:image/png;base64,aGVsbG8="
    write_json("connector-icons", {"icons": {"mcp.notion.com": {
        "dataUri": seeded, "fetchedAt": time.time()}}})

    _open_app_control(page)
    page.click("[data-testid=add-connector-menu]")
    page.click("[data-testid=browse-connectors]")
    page.wait_for_selector("[data-testid=catalog-row]")
    rows = page.locator("[data-testid=catalog-row]")
    assert rows.count() >= 5  # the five verified entries

    notion_row = page.locator("[data-testid=catalog-row]", has_text="Notion")
    assert notion_row.locator("img").get_attribute("src") == seeded

