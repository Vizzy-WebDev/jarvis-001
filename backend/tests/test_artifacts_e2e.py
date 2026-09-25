"""Artifacts, end to end in a real browser: made when asked, shown in the chat,
kept on the Artifacts page, opened back in the chat that made them, deleted.

Each test drives the built front end against a real FastAPI on a scratch port
with a stub model (`stub_provider_server.py`) that calls `create_artifact` the
way a real model would. What they pin down is the person's view of it: a card
appears with no question asked, survives a reload, opens a viewer; the page
lists it with its chat; Open in Chat lands on THAT chat's card, not the newest
chat; delete removes it everywhere; a web page Jarvis made runs sealed.
"""

from __future__ import annotations

import json

import httpx
import pytest

from stub_provider_server import StubProvider
from test_shell_e2e import CHROME, EXPORT, connect_and_select, refresh, visit

pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

from playwright.sync_api import Page, sync_playwright  # noqa: E402

pytestmark = [
    pytest.mark.skipif(not EXPORT.is_file(), reason="the front end has not been built"),
    pytest.mark.skipif(CHROME is None, reason="no Chromium in this environment"),
]


@pytest.fixture
def page(live_server):
    with sync_playwright() as play:
        browser = play.chromium.launch(executable_path=str(CHROME), args=["--no-sandbox"])
        context = browser.new_context(viewport={"width": 1440, "height": 900}, accept_downloads=True)
        page = context.new_page()
        visit(page, live_server)
        yield page
        context.close()
        browser.close()


@pytest.fixture
def model(live_server, monkeypatch):
    """A stub model that calls whatever tool `.tool` names on each new request.
    Only the latest message decides whether a tool result is already in hand,
    so one conversation can make several files."""
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


def make(page: Page, model: StubProvider, **args) -> None:
    before = page.locator("[data-testid=file-tile]").count()
    model.tool = ("create_artifact", args)
    send(page, f"make {args['filename']} for me")
    page.wait_for_function(
        f"document.querySelectorAll('[data-testid=file-tile]').length > {before}", timeout=20000)
    model.tool = None


def active(base: str) -> str:
    return httpx.get(f"{base}/api/conversations", timeout=10).json()["activeId"]


# --- in the chat -------------------------------------------------------------------------------

def test_asking_for_a_file_makes_it_at_once_and_its_card_opens_it(model, page, live_server):
    make(page, model, filename="plan.md", title="Launch plan", content="# Launch plan\n\n- Permits\n- Suppliers")
    assert page.locator("[data-testid=approval]").count() == 0  # no confirmation step
    card = page.locator("[data-testid=file-tile]").first
    assert "Launch plan" in card.inner_text() and "Markdown" in card.inner_text()

    card.locator("[data-testid=artifact-open]").click()
    page.wait_for_selector("[data-testid=artifact-panel] [data-testid=markdown] h2")
    assert page.locator("[data-testid=artifact-panel] h2").first.inner_text() == "Launch plan"
    assert page.locator("[data-testid=modal]").bounding_box()["width"] > 900  # over the page, not the panel
    with page.expect_download() as download:
        page.click("[data-testid=artifact-download]")
    assert download.value.suggested_filename == "plan.md"
    page.keyboard.press("Escape")

    refresh(page)
    page.wait_for_selector("[data-testid=file-tile]")
    assert "Launch plan" in page.locator("[data-testid=file-tile]").first.inner_text()


def test_what_an_allowed_action_made_appears_and_survives_a_reload(model, page, live_server):
    model.reply = "Running it."
    model.tool = ("run_code", {"code": "open('totals.csv','w').write('day,cups\\nMon,120\\n')"})
    send(page, "work out the totals with code")
    page.wait_for_selector("[data-testid=approval]")
    model.tool = None
    page.click("[data-testid=approve]")
    page.wait_for_selector("[data-testid=file-tile]")
    assert "totals.csv" in page.locator("[data-testid=file-tile]").inner_text()
    refresh(page)
    page.wait_for_selector("[data-testid=file-tile]")
    assert page.locator("[data-testid=file-tile]").count() == 1


# --- the Artifacts page -------------------------------------------------------------------------

def test_open_in_chat_goes_to_the_conversation_that_made_it_not_the_newest(model, page, live_server):
    make(page, model, filename="menu.md", title="Menu from chat A", content="# Menu")
    chat_a = active(live_server)
    httpx.post(f"{live_server}/api/conversations", timeout=10)
    visit(page, live_server)
    make(page, model, filename="sales.csv", title="Sales from chat B", content="day,cups\nMon,1")
    chat_b = active(live_server)
    assert chat_a != chat_b

    visit(page, f"{live_server}/#/artifacts")
    page.wait_for_function("document.querySelectorAll('[data-testid=artifact-row]').length === 2")
    rows = page.locator("[data-testid=artifact-row]")
    assert [t.split("\n")[1] for t in rows.all_inner_texts()] == ["Sales from chat B", "Menu from chat A"]
    rows.filter(has_text="Menu from chat A").click()
    page.wait_for_selector("[data-testid=artifact-detail] [data-testid=markdown]")
    page.click("[data-testid=artifact-open-chat]")

    page.wait_for_selector("[data-testid=transcript] [data-artifact-id][data-focused=true]")
    assert active(live_server) == chat_a
    assert "Menu from chat A" in page.locator("[data-testid=transcript] [data-testid=file-tile]").inner_text()


def test_delete_asks_first_then_removes_it_everywhere(model, page, live_server):
    make(page, model, filename="gone.md", title="Soon gone", content="# bye")
    artifact_id = page.locator("[data-testid=file-tile]").first.get_attribute("data-artifact-id")

    visit(page, f"{live_server}/#/artifacts/{artifact_id}")
    page.wait_for_selector("[data-testid=artifact-title]")
    page.click("[data-testid=artifact-delete]")
    page.click("[data-testid=artifact-delete-confirm]")
    page.wait_for_function("!document.querySelector('[data-testid=artifact-row]')")
    assert httpx.get(f"{live_server}/api/artifacts/{artifact_id}", timeout=10).status_code == 404

    visit(page, live_server)
    page.wait_for_selector(f"[data-artifact-id='{artifact_id}'][data-state=deleted]")


def test_a_file_with_no_chat_says_why_open_in_chat_is_unavailable(page, live_server):
    from jarvis.tools.create_artifact import _run

    made = _run(filename="nightly.md", content="# report")  # as a background job would: no chat
    visit(page, f"{live_server}/#/artifacts/{made['id']}")
    page.wait_for_selector("[data-testid=artifact-no-chat]")
    assert page.locator("[data-testid=artifact-open-chat]").is_disabled()
    assert "background job" in page.locator("[data-testid=artifact-no-chat]").inner_text()


def test_a_web_page_jarvis_made_runs_but_cannot_reach_anything(model, page, live_server):
    make(page, model, filename="tool.html", title="Tip tool", content=(
        "<!doctype html><button id=b onclick=\"this.textContent='clicked'\">go</button><p id=o></p>"
        f"<script>fetch('{live_server}/api/artifacts').then(()=>o.textContent='reached')"
        ".catch(()=>o.textContent='blocked');"
        "try{parent.document.title;o.dataset.parent='read'}catch(e){o.dataset.parent='blocked'}</script>"))
    page.locator("[data-testid=file-tile] [data-testid=artifact-open]").first.click()
    frame = page.frame_locator("[data-testid=artifact-frame]")
    frame.locator("#b").click()
    assert frame.locator("#b").inner_text() == "clicked"
    page.wait_for_timeout(500)
    assert frame.locator("#o").inner_text() == "blocked"
    assert frame.locator("#o").get_attribute("data-parent") == "blocked"
