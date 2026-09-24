"""Content Management over real HTTP: the agent API (multipart with real files),
the screen's routes, media serving headers, and refusals as plain 400/404s."""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from jarvis.content_manager import files

from content_samples import png

WEBM = b"\x1aE\xdf\xa3" + b"\x00" * 64


@pytest.fixture
def client(scratch):
    from jarvis.main import create_app

    return TestClient(create_app())


def _submit_video(client, name="3 Squat Mistakes Killing Your Gains"):
    item = {"name": name, "contentType": "video", "niche": "Fitness", "producer": "ClipBot",
            "fields": {"title": "3 Squat Mistakes", "caption": "Stop doing these", "hashtags": ["#squat"]},
            "media": [{"file": "clip.webm", "role": "primary"}, {"file": "thumb", "role": "thumbnail"}],
            "platforms": [{"platform": "tiktok"}, {"platform": "youtube", "destination": "Shorts"}]}
    response = client.post("/api/content-items", data={"item": json.dumps(item)},
                           files=[("video", ("clip.webm", WEBM, "video/webm")),
                                  ("thumb", ("thumb.png", png(), "image/png"))])
    assert response.status_code == 200, response.text
    return response.json()["item"]


def test_multipart_submission_stores_real_files_and_serves_them_safely(client):
    item = _submit_video(client)
    assert item["stage"] == "review"
    assert [p["platform"] for p in item["placements"]] == ["tiktok", "youtube"]
    thumb = next(m for m in item["media"] if m["role"] == "thumbnail")
    served = client.get(thumb["url"])
    assert served.status_code == 200 and served.content == png()
    assert served.headers["content-type"] == "image/png"
    assert served.headers["content-disposition"].startswith("attachment;")
    assert served.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in served.headers["content-security-policy"]
    video = next(m for m in item["media"] if m["role"] == "primary")
    assert client.get(video["url"]).headers["content-type"] == "video/webm"


def test_media_ids_are_shape_checked(client):
    for bad in ("../../jarvis.db", "cmf_zzzzzzzzzzzz", "cmf_000000000000"):
        assert client.get(f"/api/content-media/{bad}").status_code == 404


def test_a_refused_submission_leaves_no_orphan_files(client, scratch):
    item = {"name": "x", "contentType": "video", "media": [{"file": "clip.webm"}],
            "fields": {"thumbnail": "nope"}}
    response = client.post("/api/content-items", data={"item": json.dumps(item)},
                           files=[("clip.webm", ("clip.webm", WEBM, "video/webm"))])
    assert response.status_code == 400 and "doesn't have" in response.json()["error"]
    assert list(files.media_dir().iterdir()) == []


def test_a_listed_file_that_was_not_uploaded_is_refused(client):
    item = {"name": "x", "contentType": "video", "media": [{"file": "missing.mp4"}]}
    response = client.post("/api/content-items", data={"item": json.dumps(item)})
    assert response.status_code == 400 and "wasn't uploaded" in response.json()["error"]


def test_json_submission_and_the_separate_upload_route(client):
    uploaded = client.post("/api/content-files", files=[("a", ("s1.png", png(), "image/png")),
                                                        ("b", ("s2.png", png(rgb=(1, 2, 3)), "image/png"))])
    ids = [f["fileId"] for f in uploaded.json()["files"]]
    response = client.post("/api/content-items", json={
        "name": "5 Money Rules", "contentType": "carousel", "niche": "Finance",
        "fields": {"caption": "Save this"},
        "media": [{"fileId": i, "role": "slide", "order": n} for n, i in enumerate(ids)]})
    assert response.status_code == 200
    assert [m["fileId"] for m in response.json()["item"]["media"]] == ids


def test_full_agent_round_trip_over_http(client):
    item = _submit_video(client)
    item_id = item["id"]

    # The person asks for changes.
    r = client.post(f"/api/content-items/{item_id}/request-changes",
                    json={"what": "Stronger hook", "why": "Drop-off at 2s", "assignee": "agent"})
    assert r.status_code == 200 and r.json()["item"]["stage"] == "changes_requested"

    # The agent finds it, picks it up, and hands in a revision with a new file.
    requests = client.get("/api/content-change-requests").json()["requests"]
    assert len(requests) == 1 and requests[0]["item"]["name"] == item["name"]
    assert client.post(f"/api/content-change-requests/{requests[0]['id']}/pick-up",
                       json={"by": "ClipBot"}).json()["request"]["status"] == "in_progress"
    revision = {"fields": {"caption": "Fix these today"}, "note": "New hook",
                "by": "ClipBot", "media": [{"file": "v2.webm", "role": "primary"},
                                           {"fileId": next(m["fileId"] for m in item["media"]
                                                           if m["role"] == "thumbnail"), "role": "thumbnail"}]}
    r = client.post(f"/api/content-items/{item_id}/revisions", data={"item": json.dumps(revision)},
                    files=[("v2.webm", ("v2.webm", WEBM + b"v2", "video/webm"))])
    assert r.status_code == 200, r.text
    assert r.json()["item"]["revision"] == 2 and r.json()["item"]["stage"] == "review"

    # Approve, schedule one platform, post the other now.
    assert client.post(f"/api/content-items/{item_id}/approve").json()["item"]["stage"] == "approved"
    detail = client.get(f"/api/content-items/{item_id}").json()["item"]
    tiktok, youtube = detail["placements"]
    r = client.post(f"/api/content-placements/{tiktok['id']}/schedule",
                    json={"scheduledAt": "2099-01-01T09:00:00.000Z", "timezone": "Africa/Lagos"})
    assert r.status_code == 200 and r.json()["placement"]["status"] == "scheduled"
    assert client.post(f"/api/content-placements/{youtube['id']}/post-now").json()["item"]["stage"] == "scheduling"

    # The publisher.
    queue = client.get("/api/content-publish-queue").json()["queue"]
    assert [q["platform"] for q in queue] == ["youtube"]
    assert queue[0]["destination"] == "Shorts" and queue[0]["media"]
    assert client.post(f"/api/content-placements/{youtube['id']}/claim", json={"by": "PostBot"}).status_code == 200
    assert client.post(f"/api/content-placements/{youtube['id']}/claim", json={"by": "Other"}).status_code == 400
    r = client.post(f"/api/content-placements/{youtube['id']}/result",
                    json={"ok": True, "url": "https://youtube.com/shorts/abc", "by": "PostBot"})
    assert r.json()["placement"]["publishedUrl"] == "https://youtube.com/shorts/abc"
    # TikTok is still scheduled, so the item is still in Scheduling.
    assert client.get(f"/api/content-items/{item_id}").json()["item"]["stage"] == "scheduling"
    client.post(f"/api/content-placements/{tiktok['id']}/unschedule")
    assert client.get(f"/api/content-items/{item_id}").json()["item"]["stage"] == "published"

    # History tells the whole story.
    kinds = [e["kind"] for e in client.get(f"/api/content-items/{item_id}").json()["item"]["events"]]
    for expected in ("submitted", "changes_requested", "revision_started", "revision_submitted",
                     "approved", "scheduled", "queued", "publishing", "published", "unscheduled"):
        assert expected in kinds

    # Archive, bin, restore, forever.
    assert client.post(f"/api/content-items/{item_id}/archive").json()["item"]["stage"] == "archived"
    assert [i["id"] for i in client.get("/api/content-items?stage=archived&niche=Fitness&type=video")
            .json()["items"]] == [item_id]
    assert client.delete(f"/api/content-items/{item_id}").json()["item"]["deletedAt"]
    assert client.get("/api/content-items/summary").json()["counts"]["bin"] == 1
    assert client.post(f"/api/content-items/{item_id}/restore").json()["item"]["stage"] == "archived"
    client.delete(f"/api/content-items/{item_id}")
    assert client.delete(f"/api/content-items/{item_id}/permanent").json() == {"ok": True}
    assert client.get(f"/api/content-items/{item_id}").status_code == 404
    assert list(files.media_dir().iterdir()) == []


def test_refusals_are_plain_400s_and_missing_things_404(client):
    item = _submit_video(client)
    r = client.post(f"/api/content-items/{item['id']}/request-changes", json={"what": ""})
    assert r.status_code == 400 and r.json() == {"ok": False, "error": "Say what needs to change."}
    assert client.post("/api/content-items/nope/approve").status_code == 404
    assert client.post("/api/content-placements/nope/post-now").status_code == 404
    assert client.get("/api/content-items/nope").status_code == 404
    r = client.post(f"/api/content-placements/{item['placements'][0]['id']}/schedule",
                    json={"scheduledAt": "2099-01-01T09:00:00Z"})
    assert r.status_code == 400 and "approved" in r.json()["error"]
    assert client.post(f"/api/content-items/{item['id']}/permanent").status_code in (404, 405)


def test_meta_accounts_calendar_and_empty_bin(client):
    meta = client.get("/api/content-meta").json()
    assert "video" in meta["types"] and "tiktok" in meta["platforms"]
    assert [s["id"] for s in meta["stages"]][0] == "review"
    account = client.post("/api/content-accounts", json={
        "platform": "tiktok", "handle": "@fitwithvin", "destinations": ["Main"], "defaultNiche": "Fitness"})
    assert account.status_code == 200
    assert client.get("/api/content-meta").json()["accounts"][0]["handle"] == "@fitwithvin"
    assert client.get("/api/content-items/calendar").status_code == 400
    assert client.get("/api/content-items/calendar?start=2020-01-01T00:00:00Z&end=2100-01-01T00:00:00Z") \
        .json() == {"entries": []}
    item = _submit_video(client)
    client.delete(f"/api/content-items/{item['id']}")
    assert client.delete("/api/content-items/trash").json() == {"ok": True, "removed": 1}
