"""Content Management, end to end, in a real browser against the real app.

Realistic content (a real, playable WebM recorded in Chromium itself; real
PNGs), realistic actors: the AGENTS act over the local HTTP API exactly as an
outside program would, and the PERSON acts only through the screen. Every step
is checked against what the backend actually stored — not against what the
screen claims — and the persistence test restarts a real `jarvis.main` process
on the same data and checks everything is still where it was.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from content_samples import png
from test_shell_e2e import CHROME, EXPORT, visit

pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

from playwright.sync_api import Page, expect, sync_playwright  # noqa: E402

pytestmark = [
    pytest.mark.skipif(not EXPORT.is_file(), reason="the front end has not been built"),
    pytest.mark.skipif(CHROME is None, reason="no Chromium in this environment"),
]

BACKEND = Path(__file__).resolve().parent.parent

RECORD_WEBM = """
async (label) => {
  const c = document.createElement('canvas'); c.width = 320; c.height = 180;
  const g = c.getContext('2d');
  const rec = new MediaRecorder(c.captureStream(24), { mimeType: 'video/webm' });
  const parts = [];
  rec.ondataavailable = (e) => parts.push(e.data);
  let f = 0;
  const t = setInterval(() => {
    g.fillStyle = `hsl(${(f * 9) % 360},70%,45%)`; g.fillRect(0, 0, 320, 180);
    g.fillStyle = '#fff'; g.font = '24px sans-serif'; g.fillText(label + ' ' + f, 20, 100); f++;
  }, 42);
  rec.start();
  await new Promise((r) => setTimeout(r, 1500));
  rec.stop(); clearInterval(t);
  await new Promise((r) => (rec.onstop = r));
  const bytes = new Uint8Array(await new Blob(parts, { type: 'video/webm' }).arrayBuffer());
  let s = ''; for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
  return btoa(s);
}
"""


def record_webm(page: Page, label: str) -> bytes:
    data = base64.b64decode(page.evaluate(RECORD_WEBM, label))
    assert data[:4] == b"\x1aE\xdf\xa3" and len(data) > 5000, "Chromium did not record a real WebM"
    return data


class Agent:
    """An outside program on this PC, using only the documented local API."""

    def __init__(self, base: str, name: str):
        self.http = httpx.Client(base_url=base, timeout=30)
        self.name = name

    def submit(self, item: dict, files: list[tuple[str, bytes, str]] = ()) -> dict:
        if files:
            r = self.http.post("/api/content-items", data={"item": json.dumps(item)},
                               files=[(n, (n, b, m)) for n, b, m in files])
        else:
            r = self.http.post("/api/content-items", json=item)
        assert r.status_code == 200, r.text
        return r.json()["item"]

    def item(self, item_id: str) -> dict:
        return self.http.get(f"/api/content-items/{item_id}").json()["item"]

    def open_requests(self) -> list[dict]:
        return self.http.get("/api/content-change-requests").json()["requests"]

    def pick_up(self, request_id: str) -> None:
        r = self.http.post(f"/api/content-change-requests/{request_id}/pick-up", json={"by": self.name})
        assert r.status_code == 200, r.text

    def revise(self, item_id: str, revision: dict, files=()) -> dict:
        r = self.http.post(f"/api/content-items/{item_id}/revisions", data={"item": json.dumps(revision)},
                           files=[(n, (n, b, m)) for n, b, m in files])
        assert r.status_code == 200, r.text
        return r.json()["item"]

    def queue(self) -> list[dict]:
        return self.http.get("/api/content-publish-queue").json()["queue"]

    def claim(self, placement_id: str) -> None:
        r = self.http.post(f"/api/content-placements/{placement_id}/claim", json={"by": self.name})
        assert r.status_code == 200, r.text

    def report(self, placement_id: str, **body) -> None:
        r = self.http.post(f"/api/content-placements/{placement_id}/result", json={**body, "by": self.name})
        assert r.status_code == 200, r.text


def placement(item: dict, platform: str) -> dict:
    return next(p for p in item["placements"] if p["platform"] == platform)


def row(page: Page, platform: str):
    return page.locator(f"[data-testid=ws-placement][data-platform={platform}]")


def workspace(page: Page):
    return page.locator("[data-testid=workspace]")


def open_card(page: Page, name: str) -> None:
    page.locator(f"[data-testid=content-card][data-name='{name}']").click()
    expect(workspace(page)).to_be_visible()


def close_workspace(page: Page) -> None:
    page.locator("[data-testid=modal-close]").last.click()
    expect(workspace(page)).to_have_count(0)


def stage_count(page: Page, stage: str, count: int) -> None:
    expect(page.locator(f"[data-testid=stage-{stage}]")).to_have_attribute("data-count", str(count), timeout=10000)


def utc_for(date: str, clock: str, zone: str) -> str:
    local = datetime.fromisoformat(f"{date}T{clock}:00").replace(tzinfo=ZoneInfo(zone))
    utc = local.astimezone(timezone.utc)
    return f"{utc.strftime('%Y-%m-%dT%H:%M:%S')}.000Z"


def days_ahead(n: int) -> str:
    return (datetime.now() + timedelta(days=n)).strftime("%Y-%m-%d")


def schedule_in_dialog(page: Page, date: str, clock: str, zone: str | None = None) -> None:
    page.fill("[data-testid=schedule-date]", date)
    page.fill("[data-testid=schedule-time]", clock)
    if zone:
        page.select_option("[data-testid=schedule-zone]", zone)
    page.click("[data-testid=schedule-save]")
    expect(page.locator("[data-testid=schedule-save]")).to_have_count(0)


@pytest.fixture
def browser_page(live_server):
    with sync_playwright() as play:
        browser = play.chromium.launch(executable_path=str(CHROME), args=["--no-sandbox"])
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        visit(page, live_server)
        yield page, live_server
        context.close()
        browser.close()


VIDEO_NAME = "3 Squat Mistakes Killing Your Gains"


def test_the_whole_lifecycle_through_the_screen(browser_page, scratch):
    page, base = browser_page
    clipbot, postbot = Agent(base, "ClipBot"), Agent(base, "PostBot")

    # --- the agents hand in three realistic pieces of content ---------------------
    webm = record_webm(page, "Squat")
    video = clipbot.submit({
        "name": VIDEO_NAME, "contentType": "video", "niche": "Fitness", "producer": "ClipBot",
        "fields": {"title": "3 Squat Mistakes Killing Your Gains",
                   "description": "Most lifters make at least one of these. Fix them today.",
                   "caption": "Stop doing these 3 things under the bar",
                   "hashtags": ["#squat", "#gymtips", "#legday"], "tags": ["squat", "form"]},
        "media": [{"file": "squat.webm", "role": "primary"}, {"file": "thumb.png", "role": "thumbnail"}],
        "findings": [{"level": "warning", "text": "Hook lands at 0:03 — consider a faster open"}],
        "platforms": [{"platform": "tiktok"}, {"platform": "youtube", "destination": "Shorts"}],
    }, [("squat.webm", webm, "video/webm"), ("thumb.png", png(640, 360, (200, 60, 40)), "image/png")])
    Agent(base, "Writer").submit({
        "name": "Why AI agents still need a human reviewer", "contentType": "text_post", "niche": "AI",
        "producer": "Writer", "fields": {"body": "Agents are fast.\n\nReview keeps them honest.",
                                         "hashtags": ["#AI"]}})
    Agent(base, "SlideSmith").submit({
        "name": "5 Money Rules for Your 20s", "contentType": "carousel", "niche": "Finance",
        "producer": "SlideSmith", "fields": {"caption": "Save this for payday"},
        "media": [{"file": f"slide{i}.png", "role": "slide", "order": i} for i in range(3)],
    }, [(f"slide{i}.png", png(540, 540, c), "image/png")
        for i, c in enumerate([(20, 90, 60), (30, 60, 140), (140, 90, 20)])])

    visit(page, f"{base}/#/content/all")
    stage_count(page, "review", 3)
    expect(page.locator("[data-testid=content-needs]")).to_contain_text("3 to review")

    # Niche is a filter over the one workflow, and the counts follow it.
    page.select_option("[data-testid=filter-niche]", "Fitness")
    stage_count(page, "review", 1)
    expect(page.locator("[data-testid=content-card]")).to_have_count(1)
    page.select_option("[data-testid=filter-niche]", "")
    stage_count(page, "review", 3)

    # The carousel's slides really render and page.
    open_card(page, "5 Money Rules for Your 20s")
    expect(page.locator("[data-testid=slide-position]")).to_have_text("Slide 1 of 3")
    page.click("[data-testid=slide-next]")
    expect(page.locator("[data-testid=slide-position]")).to_have_text("Slide 2 of 3")
    expect(page.locator("[data-testid=field-body]")).to_have_count(0)  # a carousel has no body
    close_workspace(page)

    # --- REVIEW: the actual video first, then what supports it ---------------------
    open_card(page, VIDEO_NAME)
    page.wait_for_function("() => { const v = document.querySelector('[data-testid=content-video]');"
                           " return v && v.readyState >= 1 && v.videoWidth === 320 }")
    assert page.evaluate("() => document.querySelector('[data-testid=asset-thumbnail]').naturalWidth") == 640
    expect(page.locator("[data-testid=ws-findings]")).to_contain_text("Hook lands at 0:03")
    for field in ("title", "description", "caption", "hashtags", "tags"):
        expect(page.locator(f"[data-testid=field-{field}]")).to_have_count(1)
    expect(page.locator("[data-testid=field-body]")).to_have_count(0)
    expect(page.locator("[data-testid=ws-next]")).to_contain_text("You")

    page.fill("[data-testid=field-caption]", "Stop doing these 3 things under the bar — #2 wrecks knees")
    page.click("[data-testid=ws-save]")
    expect(page.locator("[data-testid=ws-save]")).to_have_count(0)
    assert clipbot.item(video["id"])["fields"]["caption"].endswith("#2 wrecks knees")

    # --- CHANGES REQUESTED, to the agent that made it --------------------------------
    page.click("[data-testid=ws-request-changes]")
    expect(page.locator("[data-testid=assign-agent]")).to_be_checked()  # media defaults to its maker
    page.fill("[data-testid=changes-what]", "Open on the knee-cave clip — the hook is too slow")
    page.fill("[data-testid=changes-why]", "Retention drops at 0:02")
    page.click("[data-testid=send-changes]")
    expect(workspace(page)).to_have_attribute("data-stage", "changes_requested")
    expect(page.locator("[data-testid=ws-request-status]")).to_contain_text("Waiting for ClipBot")
    expect(page.locator("[data-testid=field-caption]")).to_be_disabled()

    # The agent finds it over the API, picks it up — and the open screen shows it live.
    requests = clipbot.open_requests()
    assert [(r["what"], r["item"]["id"]) for r in requests] == [
        ("Open on the knee-cave clip — the hook is too slow", video["id"])]
    clipbot.pick_up(requests[0]["id"])
    expect(page.locator("[data-testid=ws-request-status]")).to_contain_text("ClipBot is working on it")

    new_cut = record_webm(page, "Knee cave")
    thumb_id = next(m["fileId"] for m in video["media"] if m["role"] == "thumbnail")
    clipbot.revise(video["id"], {
        "fields": {"caption": "Your knees caving? That's mistake #1."}, "note": "Opened on the knee-cave clip",
        "by": "ClipBot", "media": [{"file": "v2.webm", "role": "primary"},
                                   {"fileId": thumb_id, "role": "thumbnail"}]},
        [("v2.webm", new_cut, "video/webm")])

    # --- back to REVIEW, as revision 2, with the history kept ------------------------------
    expect(workspace(page)).to_have_attribute("data-stage", "review", timeout=10000)
    expect(page.locator("[data-testid=field-caption]")).to_have_value("Your knees caving? That's mistake #1.")
    expect(page.locator("[data-testid=ws-revisions] li")).to_have_count(2)
    page.click("[data-testid=view-revision-1]")
    expect(page.locator("[data-testid=viewing-revision]")).to_be_visible()
    expect(page.locator("[data-testid=field-caption]")).to_have_value(
        "Stop doing these 3 things under the bar — #2 wrecks knees")  # what you reviewed, your edit included
    page.click("[data-testid=viewing-revision] button")
    expect(page.locator("[data-testid=ws-history]")).to_contain_text("Opened on the knee-cave clip")
    page.wait_for_function("() => { const v = document.querySelector('[data-testid=content-video]');"
                           " return v && v.readyState >= 1 }")

    # --- APPROVED / READY TO POST ---------------------------------------------------------
    page.click("[data-testid=ws-approve]")
    expect(workspace(page)).to_have_attribute("data-stage", "approved")
    expect(page.locator("[data-testid=ws-next]")).to_contain_text("post or schedule it")

    # A third platform, added right here. No account to pick: which account a
    # post goes out on is the publishing tool's business, not this screen's.
    page.click("[data-testid=ws-add-platform]")
    expect(page.locator("[data-testid=schedule-account]")).to_have_count(0)
    page.select_option("[data-testid=schedule-platform]", "instagram")
    page.fill("[data-testid=schedule-destination]", "Reels")
    page.click("[data-testid=schedule-save]")
    expect(row(page, "instagram")).to_contain_text("Reels")

    # --- SCHEDULING: two platforms, two timezones -------------------------------------------
    d3, d4 = days_ahead(3), days_ahead(4)
    row(page, "tiktok").locator("[data-testid=pl-schedule]").click()
    schedule_in_dialog(page, d3, "18:30", "America/New_York")
    expect(row(page, "tiktok")).to_have_attribute("data-status", "scheduled")
    row(page, "youtube").locator("[data-testid=pl-schedule]").click()
    schedule_in_dialog(page, d4, "09:00", "Europe/London")
    expect(workspace(page)).to_have_attribute("data-stage", "scheduling")
    stored = clipbot.item(video["id"])
    assert placement(stored, "tiktok")["scheduledAt"] == utc_for(d3, "18:30", "America/New_York")
    assert placement(stored, "tiktok")["timezone"] == "America/New_York"
    assert placement(stored, "youtube")["scheduledAt"] == utc_for(d4, "09:00", "Europe/London")
    assert postbot.queue() == [], "nothing is due yet, so nothing is handed to a publisher"

    # Reschedule, then cancel one and put it back from the calendar.
    row(page, "youtube").locator("[data-testid=pl-reschedule]").click()
    schedule_in_dialog(page, d4, "11:15")
    expect(page.locator("[data-testid=ws-history]")).to_contain_text("Europe/London")
    assert placement(clipbot.item(video["id"]), "youtube")["scheduledAt"] == utc_for(d4, "11:15", "Europe/London")
    row(page, "tiktok").locator("[data-testid=pl-cancel]").click()
    expect(row(page, "tiktok")).to_have_attribute("data-status", "draft")
    close_workspace(page)

    page.click("[data-testid=stage-scheduling]")
    expect(page.locator(f"[data-testid=content-card][data-name='{VIDEO_NAME}'] [data-testid=card-state]")) \
        .to_contain_text("YouTube")
    page.click("[data-testid=mode-calendar]")
    target = datetime.now() + timedelta(days=4)
    for _ in range((target.year - datetime.now().year) * 12 + target.month - datetime.now().month):
        page.click("[data-testid=cal-next]")
    youtube_day = page.locator(f"[data-testid=cal-day][data-date='{d4}'] [data-testid=cal-entry]")
    expect(youtube_day).to_have_count(1)
    page.goto(f"{base}/#/content/all")  # back to this month for the next pick
    page.reload()
    page.click("[data-testid=stage-scheduling]")
    page.click("[data-testid=mode-calendar]")
    target = datetime.now() + timedelta(days=3)
    for _ in range((target.year - datetime.now().year) * 12 + target.month - datetime.now().month):
        page.click("[data-testid=cal-next]")
    page.locator(f"[data-testid=cal-day][data-date='{d3}']").click(position={"x": 60, "y": 70})
    page.locator("[data-testid=pick-ready-item]", has_text=VIDEO_NAME).click()
    expect(page.locator("[data-testid=schedule-target]")).to_be_visible()
    page.select_option("[data-testid=schedule-target]", label="TikTok")
    expect(page.locator("[data-testid=schedule-date]")).to_have_value(d3)
    schedule_in_dialog(page, d3, "20:00", "Africa/Lagos")
    expect(row(page, "tiktok")).to_have_attribute("data-status", "scheduled")
    assert placement(clipbot.item(video["id"]), "tiktok")["scheduledAt"] == utc_for(d3, "20:00", "Africa/Lagos")

    # --- PUBLISHED, through the hand-off queue ------------------------------------------------
    row(page, "instagram").locator("[data-testid=pl-post-now]").click()
    expect(row(page, "instagram")).to_have_attribute("data-status", "queued")
    queue = postbot.queue()
    assert len(queue) == 1
    job = queue[0]
    assert (job["platform"], job["destination"]) == ("instagram", "Reels") and "account" not in job
    assert job["version"] == {"caption": "Your knees caving? That's mistake #1.",
                              "hashtags": ["#squat", "#gymtips", "#legday"]}
    assert {m["role"] for m in job["media"]} == {"primary", "thumbnail"}
    assert postbot.http.get(next(m["url"] for m in job["media"] if m["role"] == "primary")).content == new_cut

    postbot.claim(job["placementId"])
    expect(row(page, "instagram").locator("[data-testid=placement-status]")).to_contain_text("Being posted by PostBot")
    postbot.report(job["placementId"], ok=True, url="https://www.instagram.com/reel/SQUAT3/")
    expect(row(page, "instagram")).to_have_attribute("data-status", "published")
    expect(row(page, "instagram").locator("[data-testid=placement-link]")) \
        .to_have_attribute("href", "https://www.instagram.com/reel/SQUAT3/")

    # A post that fails, is seen, and is retried.
    row(page, "youtube").locator("[data-testid=pl-post-now]").click()
    yt = placement(clipbot.item(video["id"]), "youtube")["id"]
    postbot.claim(yt)
    postbot.report(yt, ok=False, error="Upload quota exceeded for today")
    expect(row(page, "youtube")).to_have_attribute("data-status", "failed")
    expect(row(page, "youtube").locator("[data-testid=placement-failure]")).to_have_text("Upload quota exceeded for today")
    close_workspace(page)
    expect(page.locator("[data-testid=content-needs]")).to_contain_text("1 post failed")
    page.click("[data-testid=mode-list]")
    open_card(page, VIDEO_NAME)
    row(page, "youtube").locator("[data-testid=pl-retry]").click()
    expect(row(page, "youtube")).to_have_attribute("data-status", "queued")
    postbot.claim(yt)
    postbot.report(yt, ok=True, url="https://youtube.com/shorts/SQUAT3")
    expect(row(page, "youtube")).to_have_attribute("data-status", "published")

    row(page, "tiktok").locator("[data-testid=pl-cancel]").click()
    expect(workspace(page)).to_have_attribute("data-stage", "published")
    close_workspace(page)
    stage_count(page, "published", 1)
    page.click("[data-testid=stage-published]")
    expect(page.locator("[data-testid=card-state]")).to_contain_text("Published on YouTube, Instagram")

    # --- ARCHIVED: out of the way, still findable -------------------------------------------------
    open_card(page, VIDEO_NAME)
    page.click("[data-testid=ws-archive]")
    expect(workspace(page)).to_have_attribute("data-stage", "archived")
    close_workspace(page)
    stage_count(page, "published", 0)
    stage_count(page, "archived", 1)
    page.click("[data-testid=stage-archived]")
    page.select_option("[data-testid=filter-niche]", "Fitness")
    page.click("[data-testid=type-video]")
    expect(page.locator("[data-testid=content-card]")).to_have_count(1)
    page.click("[data-testid=type-carousel]")
    expect(page.locator("[data-testid=content-card]")).to_have_count(0)
    page.click("[data-testid=type-all]")
    page.select_option("[data-testid=filter-archived-from]", "published")
    expect(page.locator("[data-testid=content-card]")).to_have_count(1)
    page.fill("[data-testid=content-search]", "knee")
    expect(page.locator("[data-testid=content-card]")).to_have_count(1)
    page.fill("[data-testid=content-search]", "")
    page.select_option("[data-testid=filter-niche]", "")
    page.select_option("[data-testid=filter-archived-from]", "")

    # --- RECYCLE BIN: delete, restore exactly where it was, delete forever -------------------------
    open_card(page, VIDEO_NAME)
    page.click("[data-testid=ws-delete]")
    expect(workspace(page)).to_have_count(0)
    stage_count(page, "bin", 1)
    stage_count(page, "archived", 0)
    page.click("[data-testid=stage-bin]")
    open_card(page, VIDEO_NAME)
    expect(workspace(page)).to_have_attribute("data-stage", "bin")
    page.click("[data-testid=ws-restore]")
    expect(workspace(page)).to_have_attribute("data-stage", "archived")
    stage_count(page, "archived", 1)
    stage_count(page, "bin", 0)
    page.click("[data-testid=ws-delete]")
    expect(workspace(page)).to_have_count(0)
    stage_count(page, "bin", 1)

    file_ids = [m["fileId"] for r in clipbot.item(video["id"])["revisions"] for m in r["media"]]
    media_dir = Path(scratch.data_dir) / "content-media"
    assert all(any(p.name.startswith(f) for p in media_dir.iterdir()) for f in file_ids)
    open_card(page, VIDEO_NAME)
    page.click("[data-testid=ws-purge]")
    expect(page.locator("[data-testid=confirm-yes]")).to_contain_text("Delete forever")
    page.click("[data-testid=confirm-yes]")
    expect(workspace(page)).to_have_count(0)
    stage_count(page, "bin", 0)
    assert clipbot.http.get(f"/api/content-items/{video['id']}").status_code == 404
    assert not any(p.name.startswith(f) for f in file_ids for p in media_dir.iterdir()), \
        "delete forever must take the files with it"
    from jarvis.db import get_db
    for table in ("cm_items", "cm_placements", "cm_change_requests", "cm_revisions", "cm_events", "cm_files"):
        column = "id" if table == "cm_items" else "item_id"
        assert get_db().execute(f"SELECT COUNT(*) FROM {table} WHERE {column} = ?",
                                (video["id"],)).fetchone()[0] == 0, table
    # The other two were untouched by all of it.
    stage_count(page, "review", 2)


def test_jarvis_revises_text_itself_and_the_bin_empties_only_when_confirmed(browser_page):
    from jarvis import assembly
    from jarvis.jobs import worker
    from scripted_model import ScriptedModel, install

    page, base = browser_page
    writer = Agent(base, "Writer")
    post = writer.submit({
        "name": "Why AI agents still need a human reviewer", "contentType": "text_post", "niche": "AI",
        "producer": "Writer", "fields": {"body": "Agents are fast. Review keeps them honest.",
                                         "hashtags": ["#AI", "#agents"]}})
    model = install(assembly, ScriptedModel())
    model.calls_tool("submit_content_revision", {
        "content_item_id": post["id"],
        "fields": {"body": "Would you ship code nobody read? Then why ship agent output nobody reviewed?"},
        "note": "Opened with a question"}, call_id="rev")
    model.says("Sent back a version that opens with a question.")
    try:
        visit(page, f"{base}/#/content/all")
        open_card(page, post["name"])
        page.click("[data-testid=ws-request-changes]")
        expect(page.locator("[data-testid=assign-jarvis]")).to_be_checked()  # text defaults to Jarvis
        page.fill("[data-testid=changes-what]", "Open with a question instead of a statement")
        page.click("[data-testid=send-changes]")

        # A real background job ran, called the tool, and the revision is on screen.
        expect(workspace(page)).to_have_attribute("data-stage", "review", timeout=20000)
        expect(page.locator("[data-testid=field-body]")).to_have_value(
            "Would you ship code nobody read? Then why ship agent output nobody reviewed?")
        expect(page.locator("[data-testid=ws-history]")).to_contain_text("Jarvis")
        stored = writer.item(post["id"])
        assert stored["revision"] == 2 and stored["fields"]["hashtags"] == ["#AI", "#agents"]
        assert stored["requests"][0]["jobId"] and stored["requests"][0]["status"] == "resolved"
        assert stored["revisions"][0]["by"] == "Jarvis"

        page.click("[data-testid=ws-delete]")
        stage_count(page, "bin", 1)
        page.click("[data-testid=stage-bin]")
        page.click("[data-testid=empty-bin]")
        page.click("[data-testid=modal-close] >> nth=-1")  # changed my mind
        stage_count(page, "bin", 1)
        page.click("[data-testid=empty-bin]")
        page.click("[data-testid=confirm-yes]")
        stage_count(page, "bin", 0)
        assert writer.http.get(f"/api/content-items/{post['id']}").status_code == 404
    finally:
        worker.join_all(timeout=10)
        assembly.reset_for_tests()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _start_jarvis(data_dir: Path, env_path: Path) -> tuple[subprocess.Popen, str]:
    port = _free_port()
    env = {**os.environ, "JARVIS_DATA_DIR": str(data_dir), "JARVIS_ENV_PATH": str(env_path), "PORT": str(port)}
    proc = subprocess.Popen([sys.executable, "-m", "jarvis.main"], cwd=str(BACKEND), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base}/api/content-meta", timeout=2).status_code == 200:
                return proc, base
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    proc.kill()
    raise RuntimeError("jarvis.main never came up")


def _stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_every_stage_survives_a_real_restart(scratch):
    """A real `jarvis.main` process, stopped and started again on the same data."""
    proc, base = _start_jarvis(scratch.data_dir, scratch.env_path)
    try:
        a = Agent(base, "ClipBot")
        png_file = [("img.png", png(320, 320, (90, 30, 160)), "image/png")]
        mk = lambda name, niche: a.submit({"name": name, "contentType": "image", "niche": niche,  # noqa: E731
                                           "fields": {"caption": name}, "producer": "ClipBot",
                                           "media": [{"file": "img.png"}], "platforms": ["instagram"]}, png_file)
        review, changes, ready, sched, pub, arch, binned = (
            mk("Stays in review", "A"), mk("Changes asked", "B"), mk("Ready one", "C"), mk("Scheduled one", "D"),
            mk("Published one", "E"), mk("Archived one", "F"), mk("Binned one", "G"))
        h = a.http
        h.post(f"/api/content-items/{changes['id']}/request-changes", json={"what": "Brighter", "why": "Too dark"})
        a.pick_up(a.open_requests()[0]["id"])
        for item in (ready, sched, pub, arch):
            h.post(f"/api/content-items/{item['id']}/approve")
        when = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:00.000Z")
        h.post(f"/api/content-placements/{sched['placements'][0]['id']}/schedule",
               json={"scheduledAt": when, "timezone": "Europe/Paris"})
        h.post(f"/api/content-placements/{pub['placements'][0]['id']}/mark-posted", json={"url": "https://x.test/p"})
        h.post(f"/api/content-items/{arch['id']}/archive")
        h.delete(f"/api/content-items/{binned['id']}")
        before = {i["id"]: a.item(i["id"]) for i in (review, changes, ready, sched, pub, arch)}
    finally:
        _stop(proc)

    proc, base = _start_jarvis(scratch.data_dir, scratch.env_path)
    try:
        a = Agent(base, "ClipBot")
        for item_id, old in before.items():
            now = a.item(item_id)
            for key in ("stage", "revision", "fields", "niche", "archivedFrom", "approvedAt"):
                assert now[key] == old[key], (item_id, key)
            assert [(p["status"], p["scheduledAt"], p["timezone"], p["publishedUrl"]) for p in now["placements"]] == \
                   [(p["status"], p["scheduledAt"], p["timezone"], p["publishedUrl"]) for p in old["placements"]]
            assert now["events"] == old["events"]
            assert all(a.http.get(m["url"]).status_code == 200 for m in now["media"])
        assert a.item(changes["id"])["openRequest"]["pickedUpBy"] == "ClipBot"
        assert a.item(binned["id"])["deletedAt"]

        with sync_playwright() as play:
            browser = play.chromium.launch(executable_path=str(CHROME), args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            visit(page, f"{base}/#/content/all")
            for stage, count in (("review", 1), ("changes_requested", 1), ("approved", 1), ("scheduling", 1),
                                 ("published", 1), ("archived", 1), ("bin", 1)):
                stage_count(page, stage, count)
            page.click("[data-testid=stage-scheduling]")
            open_card(page, "Scheduled one")
            expect(row(page, "instagram").locator("[data-testid=placement-time]")).to_contain_text("Europe/Paris")
            browser.close()
    finally:
        _stop(proc)
