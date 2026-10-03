"""The person doing, on the screen, what agents and Jarvis do through the API —
in a real browser, with real files, checked against what the backend stored.

Adding a finished video yourself (a real WebM recorded in Chromium, a real PNG
thumbnail) straight into a pipeline that is already busy; giving one platform
its own cover while the others share; seeing one item under every stage its
platforms are in; typing in the numbers a platform shows and finding them in
Analytics; answering a change request by handing in a revision yourself; and
changing a caption while the post is still only scheduled.
"""

from __future__ import annotations

import json

import httpx
import pytest

from content_samples import png
from test_content_e2e import (Agent, browser_page, close_workspace, days_ahead, open_card,  # noqa: F401
                              record_webm, row, schedule_in_dialog, workspace)
from test_shell_e2e import CHROME, EXPORT, visit

pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

from playwright.sync_api import Page, expect  # noqa: E402

pytestmark = [
    pytest.mark.skipif(not EXPORT.is_file(), reason="the front end has not been built"),
    pytest.mark.skipif(CHROME is None, reason="no Chromium in this environment"),
]


def stored(base: str, item_id: str) -> dict:
    return httpx.get(f"{base}/api/content-items/{item_id}", timeout=30).json()["item"]


def item_named(base: str, name: str) -> dict:
    for stage in ("review", "approved", "scheduling", "published", "changes_requested"):
        for item in httpx.get(f"{base}/api/content-items?stage={stage}", timeout=30).json()["items"]:
            if item["name"] == name:
                return stored(base, item["id"])
    raise AssertionError(f"{name} is nowhere")


def card(page: Page, name: str):
    return page.locator(f"[data-testid=content-card][data-name='{name}']")


def upload(page: Page, selector: str, name: str, data: bytes, mime: str) -> None:
    page.set_input_files(selector, files=[{"name": name, "mimeType": mime, "buffer": data}])


def test_adding_a_video_yourself_into_a_busy_pipeline_then_publishing_it_and_its_numbers(browser_page):
    page, base = browser_page
    agent = Agent(base, "ClipBot")
    # A pipeline that is already running: things an agent handed in.
    for n in range(3):
        agent.submit({"name": f"Agent post {n}", "contentType": "text_post", "niche": "Nature Sounds",
                      "fields": {"body": f"Body {n}"}})
    clip = record_webm(page, "Rain on a Tin Roof")

    visit(page, f"{base}/#/content/all")
    page.click("[data-testid=add-content]")
    page.click("[data-testid=add-video]")
    expect(page.locator("[data-testid=new-content]").last).to_be_visible()
    page.fill("[data-testid=new-name]", "Nature Sounds Video #001")
    page.select_option("[data-testid=new-type]", "video")
    page.fill("[data-testid=new-niche]", "Nature Sounds")
    page.fill("[data-testid=new-field-title]", "Rain on a Tin Roof — 10 hours")
    page.fill("[data-testid=new-field-caption]", "Fall asleep in minutes")
    page.fill("[data-testid=new-field-hashtags]", "#rain #sleep")
    upload(page, "[data-testid=new-files-pick-primary]", "rain.webm", clip, "video/webm")
    upload(page, "[data-testid=new-files-pick-thumbnail]", "thumb.png", png(), "image/png")
    expect(page.locator("[data-testid=new-files-file]")).to_have_count(2)
    for platform in ("youtube", "tiktok", "instagram"):
        page.click(f"[data-testid=new-platform-{platform}]")
    page.click("[data-testid=new-send-review]")

    # It opens straight away, and it is simply one more item in Review.
    expect(workspace(page)).to_have_attribute("data-stage", "review")
    item = item_named(base, "Nature Sounds Video #001")
    assert item["producer"] == "you" and item["niche"] == "Nature Sounds"
    assert {m["role"]: m["name"] for m in item["media"]} == {"primary": "rain.webm", "thumbnail": "thumb.png"}
    assert httpx.get(f"{base}{next(m['url'] for m in item['media'] if m['role'] == 'primary')}").content == clip
    assert [p["platform"] for p in item["placements"]] == ["youtube", "tiktok", "instagram"]
    assert item["fields"]["hashtags"] == ["#rain", "#sleep"]
    page.wait_for_function("() => { const v = document.querySelector('[data-testid=content-video]');"
                           " return v && v.readyState >= 1 }")

    # Approve, then give TikTok its OWN cover — YouTube and Instagram keep sharing.
    page.click("[data-testid=ws-approve]")
    expect(workspace(page)).to_have_attribute("data-stage", "approved")
    row(page, "tiktok").locator("[data-testid=pl-files]").click()
    expect(row(page, "tiktok").locator("[data-testid=pl-files-slots-thumbnail-source]")).to_have_text("shared")
    upload(page, "[data-testid=pl-files-slots-pick-thumbnail]", "tiktok-cover.png", png(36, 64, (220, 40, 90)),
           "image/png")
    row(page, "tiktok").locator("[data-testid=pl-files-save]").click()
    expect(row(page, "tiktok").locator("[data-testid=placement-files]")).to_contain_text("Own thumbnail")
    expect(row(page, "youtube").locator("[data-testid=placement-files]")).to_contain_text("Shared files")
    tiktok = next(p for p in stored(base, item["id"])["placements"] if p["platform"] == "tiktok")
    assert [(m["role"], m["name"]) for m in tiktok["media"]] == [("thumbnail", "tiktok-cover.png")]

    # YouTube scheduled; TikTok posted through the hand-off; Instagram left ready.
    row(page, "youtube").locator("[data-testid=pl-schedule]").click()
    schedule_in_dialog(page, days_ahead(5), "09:00", "Europe/London")
    row(page, "tiktok").locator("[data-testid=pl-post-now]").click()
    expect(row(page, "tiktok")).to_have_attribute("data-status", "queued")
    postbot = Agent(base, "PostBot")
    (job,) = postbot.queue()
    assert {m["role"]: m["name"] for m in job["media"]} == {"primary": "rain.webm", "thumbnail": "tiktok-cover.png"}
    postbot.claim(job["placementId"])
    postbot.report(job["placementId"], ok=True, url="https://www.tiktok.com/@me/video/1")
    expect(row(page, "tiktok")).to_have_attribute("data-status", "published", timeout=10_000)
    close_workspace(page)

    # One item, under every stage one of its platforms is in — each view says which.
    for stage, in_view in (("approved", ["Instagram"]), ("scheduling", ["YouTube"]), ("published", ["TikTok"])):
        page.click(f"[data-testid=stage-{stage}]")
        chips = card(page, "Nature Sounds Video #001").locator("[data-testid=card-platform][data-in-view=true]")
        expect(chips).to_have_text(in_view)
    expect(page.locator("[data-testid=content-list-count]")).to_contain_text("1 platform post published")

    # The numbers TikTok shows, typed in — then found in Analytics.
    open_card(page, "Nature Sounds Video #001")
    expect(row(page, "tiktok").locator("[data-testid=placement-numbers]")).to_contain_text("No numbers reported yet")
    row(page, "tiktok").locator("[data-testid=pl-add-numbers]").click()
    page.fill("[data-testid=numbers-views]", "12,400")
    page.fill("[data-testid=numbers-likes]", "830")
    page.fill("[data-testid=numbers-watchTimeSeconds]", "90")
    page.click("[data-testid=numbers-save]")
    expect(row(page, "tiktok").locator("[data-testid=placement-numbers]")).to_contain_text("12.4K")
    tiktok = next(p for p in stored(base, item["id"])["placements"] if p["platform"] == "tiktok")
    assert tiktok["metrics"]["values"] == {"views": 12400, "likes": 830, "watchTimeSeconds": 5400}
    assert tiktok["metrics"]["source"] == "you"
    close_workspace(page)
    page.click("[data-testid=stage-analytics]")
    table = page.locator("[data-testid=analytics-table]")
    expect(table).to_contain_text("Nature Sounds Video #001")
    expect(table.locator("[data-testid=analytics-row]")).to_have_count(1)
    expect(page.locator("[data-testid=content-analytics]")).to_contain_text("12.4K")


def test_ready_to_post_on_the_way_in_and_answering_your_own_change_request(browser_page):
    page, base = browser_page
    visit(page, f"{base}/#/content/all")

    # A carousel, added as Ready to Post: the ordinary approve step, done as it's added.
    page.click("[data-testid=add-content]")
    page.click("[data-testid=add-carousel]")
    page.fill("[data-testid=new-name]", "5 Budget Rules")
    page.select_option("[data-testid=new-type]", "carousel")
    page.fill("[data-testid=new-field-caption]", "Save this")
    page.set_input_files("[data-testid=new-files-pick-slide]", files=[
        {"name": f"slide{i}.png", "mimeType": "image/png", "buffer": png(36, 64, (40 * i, 90, 120))}
        for i in range(1, 4)])
    page.click("[data-testid=new-platform-instagram]")
    page.click("[data-testid=new-ready]")
    expect(workspace(page)).to_have_attribute("data-stage", "approved")
    item = item_named(base, "5 Budget Rules")
    assert [m["name"] for m in item["media"] if m["role"] == "slide"] == ["slide1.png", "slide2.png", "slide3.png"]
    assert [e["kind"] for e in reversed(item["events"])] == ["submitted", "approved"]

    # Your own item, sent back for changes to... you.
    page.click("[data-testid=ws-request-changes]")
    expect(page.locator("[data-testid=assign-agent]").locator("xpath=..")).to_contain_text("You")
    page.fill("[data-testid=changes-what]", "Swap the last slide and tighten the caption")
    page.click("[data-testid=send-changes]")
    expect(workspace(page)).to_have_attribute("data-stage", "changes_requested")
    expect(page.locator("[data-testid=ws-request-status]")).to_contain_text("Waiting for you")

    # Handing in the revision yourself: new caption, last slide replaced.
    page.click("[data-testid=ws-hand-in]")
    page.fill("[data-testid=revise-field-caption]", "Save this — rule 3 matters most")
    page.locator("[data-testid=revise-files-file]").nth(2).locator("[data-testid=revise-files-remove]").click()
    upload(page, "[data-testid=revise-files-pick-slide]", "slide3-better.png", png(36, 64, (200, 200, 60)),
           "image/png")
    page.fill("[data-testid=revise-note]", "New last slide")
    page.click("[data-testid=revise-save]")
    expect(workspace(page)).to_have_attribute("data-stage", "review")
    item = stored(base, item["id"])
    assert item["revision"] == 2 and item["fields"]["caption"] == "Save this — rule 3 matters most"
    assert [m["name"] for m in item["media"] if m["role"] == "slide"] == \
        ["slide1.png", "slide2.png", "slide3-better.png"]
    assert item["revisions"][0]["by"] == "you" and item["revisions"][0]["note"] == "New last slide"


def test_a_scheduled_post_can_still_be_changed_and_goes_out_changed(browser_page):
    page, base = browser_page
    agent = Agent(base, "Writer")
    post = agent.submit({"name": "Weekend plans", "contentType": "text_post",
                         "fields": {"body": "First draft", "hashtags": ["#weekend"]},
                         "platforms": [{"platform": "x"}]})
    httpx.post(f"{base}/api/content-items/{post['id']}/approve", timeout=30)
    visit(page, f"{base}/#/content/all")
    page.click("[data-testid=stage-approved]")
    open_card(page, "Weekend plans")
    row(page, "x").locator("[data-testid=pl-schedule]").click()
    schedule_in_dialog(page, days_ahead(2), "10:00", "America/New_York")
    expect(workspace(page)).to_have_attribute("data-stage", "scheduling")

    expect(page.locator("[data-testid=field-body]")).to_be_enabled()
    page.fill("[data-testid=field-body]", "Changed after scheduling")
    page.click("[data-testid=ws-save]")
    expect(page.locator("[data-testid=ws-save]")).to_have_count(0)
    assert stored(base, post["id"])["fields"]["body"] == "Changed after scheduling"
    assert stored(base, post["id"])["placements"][0]["status"] == "scheduled"
