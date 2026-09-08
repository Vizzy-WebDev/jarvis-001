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
    server.base_url = base_url = server.start()
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
    from jarvis.connectors import store

    return store.add_connector(type=kind, label=label)["id"]


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

def test_a_model_can_be_added_through_the_screen_and_then_answers(page, stub):
    """The front door: nothing works until a model is added, so this walks the
    real three steps — pick a provider, give the address, choose models — and
    then proves the thing that was added can actually hold a conversation."""
    from jarvis.gateway import registry

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
    assert "alpha" in [m["model"] for m in registry.list_models()]

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
    from jarvis.gateway import registry

    page.goto(page.url.split("#")[0] + "#/models", wait_until="networkidle")
    page.wait_for_selector("[data-testid=connection-card]")
    page.locator("[data-testid=connection-card]").first.get_by_text("Remove").click()

    # The count is stated before it happens, not discovered afterwards.
    assert "1 model" in page.locator("[data-testid=modal]").inner_text()
    page.click("[data-testid=confirm-remove]")
    page.wait_for_selector("[data-testid=connection-card]", state="detached")
    assert registry.list_models() == []


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


def test_the_realtime_engine_is_offered_from_a_capability_and_fails_honestly(voice_page):
    """Two things at once, and both are the point.

    It is offered because a connected model's adapter DECLARES a realtime API —
    no provider is named anywhere in the picker, the socket, or the engine — and
    when the session cannot actually open, the start FAILS rather than leaving
    the microphone running under a screen claiming to listen. The original
    resolved on the socket merely opening, so a session that could never start
    took the microphone first and mentioned the problem afterwards.
    """
    from jarvis.gateway import connections, registry

    conn = connections.add_connection(adapter="gemini", base_url=None, label="realtime",
                                      provider="gemini", kind="first-party", key_required=True,
                                      secret="not-a-real-key")
    registry.add_model(connection_id=conn["id"], model="a-realtime-model")

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
