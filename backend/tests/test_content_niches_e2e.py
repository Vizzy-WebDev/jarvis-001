"""Niches as folders, in a real browser, checked against what the backend stored.

The first screen is the folders. A new niche exists while empty; inside it,
"+ Add" makes one piece with the niche filled in, and "Add several files…"
makes one SEPARATE item per file. The folder survives a refresh, is renamed with
everything in it, refuses to be deleted while it holds anything, and an item
moves between folders by changing its niche. Content handed in without a niche
sits in "No niche" until it is given one.
"""

from __future__ import annotations

import httpx
import pytest

from test_content_e2e import Agent, browser_page, close_workspace, open_card, record_webm  # noqa: F401
from test_shell_e2e import CHROME, EXPORT, refresh, visit

pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

from playwright.sync_api import Page, expect  # noqa: E402

pytestmark = [
    pytest.mark.skipif(not EXPORT.is_file(), reason="the front end has not been built"),
    pytest.mark.skipif(CHROME is None, reason="no Chromium in this environment"),
]


def items_in(base: str, **query) -> list[dict]:
    return httpx.get(f"{base}/api/content-items", params={"stage": "active", **query}, timeout=30).json()["items"]


def folder_card(page: Page, name: str):
    return page.locator(f"[data-testid=niche-card][data-niche='{name}']")


def test_niche_folders_from_an_empty_one_to_many_separate_items(browser_page):
    page, base = browser_page
    agent = Agent(base, "Writer")
    for name, niche in (("Why we procrastinate", "Psychology"), ("The spotlight effect", "psychology"),
                        ("Untitled idea", "")):
        agent.submit({"name": name, "contentType": "text_post", "niche": niche, "producer": "Writer",
                      "fields": {"body": name}})
    clip = record_webm(page, "Stoicism")

    # --- the folders ---------------------------------------------------------------------------
    visit(page, f"{base}/#/content")
    expect(folder_card(page, "Psychology")).to_have_attribute("data-total", "2")   # both spellings, one folder
    expect(page.locator("[data-testid=niche-card]")).to_have_count(1)
    expect(page.locator("[data-testid=folder-none]")).to_have_attribute("data-total", "1")
    expect(page.locator("[data-testid=folder-all]")).to_have_attribute("data-total", "3")

    # --- a new, empty niche ------------------------------------------------------------------------
    page.click("[data-testid=new-niche]")
    page.fill("[data-testid=niche-name]", "Stoicism")
    page.click("[data-testid=niche-save]")
    expect(page.locator("[data-testid=folder-name]")).to_have_text("Stoicism")
    assert page.evaluate("location.hash") == "#/content/niche/Stoicism"
    expect(page.locator("[data-testid=content-screen]")).to_contain_text("Nothing in “Stoicism” yet.")

    # --- one piece, the niche filled in ----------------------------------------------------------------
    page.click("[data-testid=add-content]")
    page.click("[data-testid=add-video]")
    expect(page.locator("[data-testid=new-niche]")).to_have_value("Stoicism")
    expect(page.locator("[data-testid=new-type]")).to_have_value("video")
    page.fill("[data-testid=new-name]", "The Dichotomy of Control")
    page.set_input_files("[data-testid=new-files-pick-primary]",
                         files=[{"name": "control.webm", "mimeType": "video/webm", "buffer": clip}])
    page.click("[data-testid=new-send-review]")
    expect(page.locator("[data-testid=workspace]")).to_be_visible()
    close_workspace(page)
    expect(page.locator("[data-testid=stage-active]")).to_have_attribute("data-count", "1")

    # --- several files at once: each its own item ------------------------------------------------------
    page.click("[data-testid=add-content]")
    page.click("[data-testid=add-several]")
    expect(page.locator("[data-testid=several-niche]")).to_have_value("Stoicism")
    page.set_input_files("[data-testid=several-files]", files=[
        {"name": f"memento_mori-{n}.webm", "mimeType": "video/webm", "buffer": clip} for n in (1, 2, 3)])
    expect(page.locator("[data-testid=several-row]")).to_have_count(3)
    expect(page.locator("[data-testid=several-name]").first).to_have_value("memento mori 1")
    page.locator("[data-testid=several-name]").nth(1).fill("Amor Fati")
    page.click("[data-testid=several-platform-tiktok]")
    page.click("[data-testid=several-review]")
    expect(page.locator("[data-testid=several]")).to_have_count(0, timeout=30000)   # closes when all are in
    expect(page.locator("[data-testid=stage-active]")).to_have_attribute("data-count", "4")
    expect(page.locator("[data-testid=type-video]")).to_have_attribute("data-count", "4")

    stored = items_in(base, niche="Stoicism")
    assert sorted(i["name"] for i in stored) == ["Amor Fati", "The Dichotomy of Control", "memento mori 1",
                                                 "memento mori 3"]
    assert len({i["id"] for i in stored}) == 4
    for item in stored:
        assert item["stage"] == "review" and item["producer"] == "you" and item["contentType"] == "video"
        assert [m["role"] for m in item["media"]] == ["primary"]
    several = [i for i in stored if i["name"] != "The Dichotomy of Control"]
    assert all([p["platform"] for p in i["placements"]] == ["tiktok"] for i in several)

    # --- a refresh stays in the folder ----------------------------------------------------------------
    refresh(page)
    expect(page.locator("[data-testid=folder-name]")).to_have_text("Stoicism")
    expect(page.locator("[data-testid=stage-active]")).to_have_attribute("data-count", "4")

    # --- rename: everything moves with it ---------------------------------------------------------------
    page.click("[data-testid=rename-niche]")
    page.fill("[data-testid=niche-name]", "Stoic Philosophy")
    page.click("[data-testid=niche-save]")
    expect(page.locator("[data-testid=folder-name]")).to_have_text("Stoic Philosophy")
    expect(page.locator("[data-testid=stage-active]")).to_have_attribute("data-count", "4")
    assert len(items_in(base, niche="Stoic Philosophy")) == 4 and items_in(base, niche="Stoicism") == []
    page.click("[data-testid=rename-niche]")
    page.fill("[data-testid=niche-name]", "psychology")
    page.click("[data-testid=niche-save]")
    expect(page.locator("[data-testid=niche-error]")).to_contain_text("never merged")
    page.locator("[data-testid=modal-close]").last.click()

    # --- delete is refused while it holds anything -------------------------------------------------------
    page.click("[data-testid=delete-niche]")
    expect(page.locator("[data-testid=niche-delete-body]")).to_contain_text("still holds 4 items")
    expect(page.locator("[data-testid=niche-delete-yes]")).to_have_count(0)
    page.click("[data-testid=niche-delete-ok]")

    # --- moving one piece to another niche ---------------------------------------------------------------
    open_card(page, "Amor Fati")
    page.fill("[data-testid=ws-niche]", "Psychology")
    page.click("[data-testid=ws-save]")
    close_workspace(page)
    expect(page.locator("[data-testid=stage-active]")).to_have_attribute("data-count", "3")
    expect(page.locator("[data-testid=content-card][data-name='Amor Fati']")).to_have_count(0)

    # --- back to the folders; "No niche" empties and goes away ---------------------------------------------
    page.click("[data-testid=back-to-niches]")
    expect(folder_card(page, "Psychology")).to_have_attribute("data-total", "3")
    expect(folder_card(page, "Stoic Philosophy")).to_have_attribute("data-total", "3")
    page.click("[data-testid=folder-none]")
    expect(page.locator("[data-testid=folder-name]")).to_have_text("No niche")
    open_card(page, "Untitled idea")
    page.fill("[data-testid=ws-niche]", "Stoic Philosophy")
    page.click("[data-testid=ws-save]")
    close_workspace(page)
    page.go_back()                                                   # Back leaves the folder
    expect(page.locator("[data-testid=niche-grid]")).to_be_visible()
    expect(page.locator("[data-testid=folder-none]")).to_have_count(0)
    expect(folder_card(page, "Stoic Philosophy")).to_have_attribute("data-total", "4")

    # --- an empty niche can be deleted ---------------------------------------------------------------------
    page.click("[data-testid=new-niche]")
    page.fill("[data-testid=niche-name]", "Temporary")
    page.click("[data-testid=niche-save]")
    page.click("[data-testid=delete-niche]")
    page.click("[data-testid=niche-delete-yes]")
    expect(page.locator("[data-testid=niche-grid]")).to_be_visible()
    expect(folder_card(page, "Temporary")).to_have_count(0)
    names = [n["name"] for n in httpx.get(f"{base}/api/content-niches", timeout=30).json()["niches"]]
    assert names == ["Psychology", "Stoic Philosophy"]


def test_what_you_are_typing_survives_an_agent_handing_something_in(browser_page):
    """The screen refreshes whenever anything changes. An open item used to be
    read again on every refresh and put back as stored — wiping a caption the
    person was halfway through typing whenever an agent handed something in."""
    page, base = browser_page
    agent = Agent(base, "Writer")
    agent.submit({"name": "Why we procrastinate", "contentType": "text_post", "niche": "Psychology",
                  "producer": "Writer", "fields": {"body": "First draft."}})
    visit(page, f"{base}/#/content/niche/Psychology")
    open_card(page, "Why we procrastinate")
    page.fill("[data-testid=field-body]", "Half-way through a better version")
    page.fill("[data-testid=ws-niche]", "Psychology & Mind")

    for n in range(3):                       # the busy pipeline carries on meanwhile
        agent.submit({"name": f"Another one {n}", "contentType": "text_post", "niche": "Psychology",
                      "producer": "Writer", "fields": {"body": "x"}})
    expect(page.locator("[data-testid=stage-active]")).to_have_attribute("data-count", "4", timeout=10000)
    page.wait_for_timeout(800)

    expect(page.locator("[data-testid=field-body]")).to_have_value("Half-way through a better version")
    expect(page.locator("[data-testid=ws-niche]")).to_have_value("Psychology & Mind")
    page.click("[data-testid=ws-save]")
    expect(page.locator("[data-testid=ws-save]")).to_have_count(0)
    [item] = items_in(base, niche="Psychology & Mind")
    assert item["fields"]["body"] == "Half-way through a better version"


def test_jarvis_and_an_agent_naming_a_new_niche_make_its_folder_on_the_open_screen(browser_page):
    """Whoever names a niche that doesn't exist yet — Jarvis's own tool, or an
    agent over the local API — makes its folder, and an open screen shows it
    without a reload."""
    from jarvis.tools.content_manager_tools import _submit

    page, base = browser_page
    visit(page, f"{base}/#/content")
    expect(page.locator("[data-testid=niche-card]")).to_have_count(0)

    done = _submit(name="Stoic mornings", content_type="text_post", niche="Stoicism",
                   fields={"body": "Begin each day by telling yourself…"})
    assert done["ok"], done
    expect(folder_card(page, "Stoicism")).to_have_attribute("data-total", "1", timeout=10000)

    Agent(base, "ClipBot").submit({"name": "Night sky timelapse", "contentType": "text_post",
                                   "niche": "Astronomy", "producer": "ClipBot", "fields": {"body": "Stars."}})
    expect(folder_card(page, "Astronomy")).to_have_attribute("data-total", "1", timeout=10000)

    # And inside an open folder, the new piece arrives in its list.
    folder_card(page, "Stoicism").click()
    expect(page.locator("[data-testid=stage-active]")).to_have_attribute("data-count", "1")
    _submit(name="Memento mori", content_type="text_post", niche="stoicism", fields={"body": "Remember."})
    expect(page.locator("[data-testid=stage-active]")).to_have_attribute("data-count", "2", timeout=10000)
    expect(page.locator("[data-testid=content-card][data-name='Memento mori']")).to_have_attribute(
        "data-niche", "Stoicism")
