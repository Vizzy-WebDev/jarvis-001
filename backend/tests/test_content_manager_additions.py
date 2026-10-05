"""What the person can now do themselves, and what Content Management now holds.

The person adds content on the screen through the SAME door agents and Jarvis
use (and may mark it Ready to Post as they add it — the ordinary approve step);
changes its files; gives one platform its own file or cover while the others
share; sees an item under every stage one of its platforms is in; searches the
content rather than its storage format; pages through thousands; and records
reported numbers. Jarvis's new tools reach the same functions.

Every assertion checks what the backend stored, not what a response claimed.
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from jarvis.content_manager import files, lifecycle, store
from jarvis.content_manager.lifecycle import ContentError
from jarvis.db import get_db
from jarvis.jscompat import to_iso_z

from content_samples import png

WEBM = b"\x1aE\xdf\xa3" + b"\x00" * 64


@pytest.fixture(autouse=True)
def _isolated(scratch):
    """EVERY test here runs on a scratch data dir — a test without it writes into
    the real project `data/` (it happened while this file was being written)."""
    from jarvis import assembly

    assembly.reset_for_tests()
    yield
    assembly.reset_for_tests()


@pytest.fixture
def client(scratch):
    from jarvis.main import create_app

    return TestClient(create_app())


def _later(minutes=60):
    return to_iso_z(datetime.now(timezone.utc) + timedelta(minutes=minutes))


def _file(name, data):
    return files.save_stream(io.BytesIO(data), name)["fileId"]


def _video(**extra):
    return lifecycle.submit(
        name="Rain on a Tin Roof #001", content_type="video", niche="Nature Sounds", producer="ClipBot",
        fields={"title": "Rain on a Tin Roof", "caption": "Sleep in 5 minutes", "hashtags": ["#rain"]},
        media=[{"fileId": _file("rain.webm", WEBM), "role": "primary"},
               {"fileId": _file("thumb.png", png()), "role": "thumbnail"}], **extra)


def _publish(placement_id, url="https://example.com/p"):
    lifecycle.post_now(placement_id)
    lifecycle.claim(placement_id, by="PostBot")
    lifecycle.report_result(placement_id, ok=True, url=url, by="PostBot")


def _names(**query):
    return [i["name"] for i in store.list_items(**query)]


# --- adding content yourself -----------------------------------------------------------------

def test_the_person_adds_content_through_the_same_door_and_may_mark_it_ready(client):
    item = {"name": "My own rain video", "contentType": "video", "niche": "Nature Sounds", "producer": "you",
            "fields": {"title": "Rain", "caption": "Recorded it myself"},
            "media": [{"file": "rain.webm", "role": "primary"}, {"file": "cover.png", "role": "thumbnail"}],
            "platforms": [{"platform": "youtube"}, {"platform": "tiktok"}], "readyToPost": True}
    r = client.post("/api/content-items", data={"item": json.dumps(item)},
                    files=[("rain.webm", ("rain.webm", WEBM, "video/webm")),
                           ("cover.png", ("cover.png", png(), "image/png"))])
    assert r.status_code == 200, r.text
    stored = store.item_detail(r.json()["item"]["id"])
    assert stored["stage"] == "approved" and stored["approvedAt"]
    assert [e["kind"] for e in reversed(stored["events"])] == ["submitted", "approved"]
    assert {m["role"] for m in stored["media"]} == {"primary", "thumbnail"}
    assert [p["platform"] for p in stored["placements"]] == ["youtube", "tiktok"]
    # And straight into a pipeline that is already running: it is simply one more item.
    assert stored["id"] in [i["id"] for i in store.list_items(stage="approved")]

    in_review = client.post("/api/content-items", json={
        "name": "Written by hand", "contentType": "text_post", "fields": {"body": "Hello"}}).json()["item"]
    assert in_review["stage"] == "review"


def test_no_jarvis_tool_can_mark_content_ready(scratch):
    from jarvis import assembly

    tool = assembly.get_registry().get("submit_content_for_review")
    result = tool.handler(name="Sneaky", content_type="text_post", fields={"body": "x"},
                          approve_now=True, readyToPost=True, ready_to_post=True)
    assert store.get_item(result["contentItemId"])["stage"] == "review"


# --- changing files -----------------------------------------------------------------------------

def test_changing_an_items_files_on_the_screen(client):
    item = _video()
    primary = next(m for m in item["media"] if m["role"] == "primary")
    body = {"media": [{"fileId": primary["fileId"], "role": "primary"},
                      {"file": "better.png", "role": "thumbnail"}]}
    r = client.patch(f"/api/content-items/{item['id']}", data={"item": json.dumps(body)},
                     files=[("better.png", ("better.png", png(rgb=(200, 60, 60)), "image/png"))])
    assert r.status_code == 200, r.text
    stored = store.item_detail(item["id"])
    thumb = next(m for m in stored["media"] if m["role"] == "thumbnail")
    assert thumb["name"] == "better.png"
    assert any(m["name"] == "better.png" for m in stored["revisions"][0]["media"]), "revision 1 is what's there"

    # A refused change leaves no stray file behind.
    before = get_db().execute("SELECT COUNT(*) FROM cm_files").fetchone()[0]
    r = client.patch(f"/api/content-items/{item['id']}", data={"item": json.dumps(
        {"media": [{"file": "x.png", "role": "thumbnail"}]})},
                     files=[("x.png", ("x.png", png(), "image/png"))])
    assert r.status_code == 400 and "needs the actual file" in r.json()["error"]
    assert get_db().execute("SELECT COUNT(*) FROM cm_files").fetchone()[0] == before


def test_one_platform_can_use_its_own_files_while_the_others_share(client):
    item = _video(platforms=[{"platform": "youtube"}, {"platform": "tiktok"}, {"platform": "instagram"}])
    lifecycle.approve(item["id"])
    by_platform = {p["platform"]: p for p in store.get_item(item["id"])["placements"]}

    # TikTok: the same video, its own cover. Instagram: its own (vertical) video.
    cover = client.patch(f"/api/content-placements/{by_platform['tiktok']['id']}",
                         data={"item": json.dumps({"media": [{"file": "tt.png", "role": "thumbnail"}]})},
                         files=[("tt.png", ("tt.png", png(36, 64), "image/png"))])
    assert cover.status_code == 200, cover.text
    lifecycle.update_placement(by_platform["instagram"]["id"],
                               media=[{"fileId": _file("vertical.webm", WEBM), "role": "primary"}])

    current = store.get_item(item["id"])
    merged = {p["platform"]: {m["role"]: m["name"] for m in store.merged_media(current, p)}
              for p in current["placements"]}
    assert merged["youtube"] == {"primary": "rain.webm", "thumbnail": "thumb.png"}
    assert merged["tiktok"] == {"primary": "rain.webm", "thumbnail": "tt.png"}
    assert merged["instagram"] == {"primary": "vertical.webm", "thumbnail": "thumb.png"}

    # What a publisher is handed is exactly that.
    for p in current["placements"]:
        lifecycle.post_now(p["id"])
    queue = {j["platform"]: {m["role"]: m["name"] for m in j["media"]} for j in store.publish_queue()}
    assert queue == merged

    # Back to shared: an empty list.
    lifecycle.unschedule(by_platform["tiktok"]["id"])
    lifecycle.update_placement(by_platform["tiktok"]["id"], media=[])
    tiktok = next(p for p in store.get_item(item["id"])["placements"] if p["platform"] == "tiktok")
    assert tiktok["media"] == []
    # Delete forever still removes every file, the platform-specific ones included.
    lifecycle.unschedule(by_platform["youtube"]["id"])
    lifecycle.unschedule(by_platform["instagram"]["id"])
    paths = files.paths_for_item(item["id"])
    assert len(paths) == 4 and all(p.exists() for p in paths)
    lifecycle.delete(item["id"])
    lifecycle.purge(item["id"])
    assert not any(p.exists() for p in paths)


def test_a_platform_cannot_borrow_another_items_file():
    mine, other = _video(), _video()
    lifecycle.approve(mine["id"])
    placement = lifecycle.add_placement(mine["id"], platform="tiktok")
    theirs = other["media"][0]["fileId"]
    with pytest.raises(ContentError, match="belongs to something else"):
        lifecycle.update_placement(placement["id"], media=[{"fileId": theirs, "role": "primary"}])


# --- one item, many platforms: seen under every stage it is in ---------------------------------

def test_an_item_shows_under_every_stage_one_of_its_platforms_is_in():
    item = _video(platforms=[{"platform": "youtube"}, {"platform": "tiktok"}, {"platform": "instagram"}])
    lifecycle.approve(item["id"])
    yt, tt, ig = store.get_item(item["id"])["placements"]
    lifecycle.schedule(yt["id"], scheduled_at=_later(), timezone_name="Europe/London")
    _publish(tt["id"])
    # Instagram left Ready to Post.
    for stage in ("approved", "scheduling", "published"):
        assert _names(stage=stage) == ["Rain on a Tin Roof #001"], stage
    summary = store.summary()
    assert (summary["counts"]["approved"], summary["counts"]["scheduling"], summary["counts"]["published"]) \
        == (1, 1, 1)
    assert summary["posts"] == {"approved": 1, "scheduling": 1, "published": 1}
    # The item's own headline stage is unchanged.
    assert store.get_item(item["id"])["stage"] == "scheduling"
    # A filter to one platform narrows the posts too.
    tiktok_only = store.summary(platform="tiktok")["posts"]
    assert tiktok_only == {"approved": 0, "scheduling": 0, "published": 1}


# --- search, paging ----------------------------------------------------------------------------------

def test_search_looks_at_what_is_written_not_how_it_is_stored():
    _video()
    lifecycle.submit(name="Budget basics", content_type="text_post", niche="Finance",
                     fields={"body": "Spend less than you earn", "hashtags": ["#money"]})
    assert _names(q="title") == []          # a field NAME, which every video has
    assert _names(q="Tin Roof") == ["Rain on a Tin Roof #001"]
    assert _names(q="sleep in 5") == ["Rain on a Tin Roof #001"]   # the caption
    assert _names(q="#money") == ["Budget basics"]                  # a hashtag
    assert _names(q="finance") == ["Budget basics"]                 # the niche


def test_paging_through_a_busy_stage(client):
    for n in range(7):
        lifecycle.submit(name=f"Post {n}", content_type="text_post", fields={"body": f"Body {n}"})
    first = client.get("/api/content-items?stage=review&limit=3").json()
    second = client.get("/api/content-items?stage=review&limit=3&offset=3").json()
    third = client.get("/api/content-items?stage=review&limit=3&offset=6").json()
    assert first["total"] == 7 and len(first["items"]) == 3 and len(third["items"]) == 1
    seen = [i["name"] for page in (first, second, third) for i in page["items"]]
    assert seen == [f"Post {n}" for n in range(7)]  # Review is a queue: oldest first, nothing twice
    assert client.get("/api/content-items?stage=review&limit=x").status_code == 400
    assert "total" not in client.get("/api/content-items?stage=review").json()


# --- reported numbers ---------------------------------------------------------------------------------

def test_numbers_are_dated_snapshots_of_what_was_reported(client):
    item = _video(platforms=[{"platform": "youtube"}, {"platform": "tiktok"}])
    lifecycle.approve(item["id"])
    yt, tt = store.get_item(item["id"])["placements"]
    with pytest.raises(ContentError, match="has been published"):
        lifecycle.record_metrics(yt["id"], metrics={"views": 10})
    _publish(yt["id"], "https://youtube.example/v/1")
    _publish(tt["id"], "https://tiktok.example/v/1")

    for bad, why in (({}, "as an object"), ({"views": -1}, "zero or more"), ({"views": "lots"}, "a number"),
                     ({"views": True}, "a number"), ({"<script>": 1}, "isn't a name")):
        with pytest.raises(ContentError, match=why):
            lifecycle.record_metrics(yt["id"], metrics=bad)

    day1 = "2026-10-01T09:00:00+01:00"
    lifecycle.record_metrics(yt["id"], metrics={"views": 120, "likes": 9}, captured_at=day1, by="PostBot")
    r = client.post(f"/api/content-placements/{yt['id']}/metrics",
                    json={"metrics": {"views": 1500, "likes": 80, "watchTimeSeconds": 5400.5,
                                      "clickThroughs": 12}, "capturedAt": "2026-10-30T09:00:00Z", "by": "you"})
    assert r.status_code == 200, r.text
    # A late report of an OLDER day doesn't replace the latest numbers.
    lifecycle.record_metrics(yt["id"], metrics={"views": 400}, captured_at="2026-10-10T09:00:00Z")

    history = client.get(f"/api/content-placements/{yt['id']}/metrics").json()["history"]
    assert [h["values"]["views"] for h in history] == [1500, 400, 120]
    latest = next(p for p in store.get_item(item["id"])["placements"] if p["id"] == yt["id"])["metrics"]
    assert latest["values"] == {"views": 1500, "likes": 80, "watchTimeSeconds": 5400.5, "clickThroughs": 12}
    assert latest["capturedAt"] == "2026-10-30T09:00:00.000Z" and latest["source"] == "you"

    report = client.get("/api/content-analytics").json()
    assert report["reported"] == 1 and len(report["posts"]) == 2
    assert report["totals"] == {"views": 1500, "likes": 80, "watchTimeSeconds": 5400.5, "clickThroughs": 12}
    tiktok_row = next(p for p in report["posts"] if p["platform"] == "tiktok")
    assert tiktok_row["metrics"] is None, "nobody reported TikTok numbers — none shown, not zeros"
    by_platform = {g["platform"]: g for g in report["byPlatform"]}
    assert by_platform["tiktok"]["reported"] == 0 and by_platform["youtube"]["totals"]["views"] == 1500
    assert client.get("/api/content-analytics?platform=tiktok").json()["totals"] == {}


# --- Jarvis reaches the same functions --------------------------------------------------------------

def test_jarvis_hands_in_a_file_the_person_attached_in_chat(scratch):
    from jarvis import assembly, uploads

    upload = uploads.save_upload(WEBM, "my-rain.webm")
    tool = assembly.get_registry().get("submit_content_for_review")
    result = tool.handler(name="Rain from chat", content_type="video", niche="Nature Sounds",
                          fields={"title": "Rain"}, uploads=[{"upload_id": upload["id"], "role": "primary"}])
    assert result["ok"] is True, result
    stored = store.get_item(result["contentItemId"])
    assert [m["name"] for m in stored["media"]] == ["my-rain.webm"]
    # A copy, so the chat upload being pruned later doesn't take the content with it.
    assert files.get(stored["media"][0]["fileId"])[1].read_bytes() == WEBM
    gone = tool.handler(name="x", content_type="video", uploads=[{"upload_id": "nope"}])
    assert gone["ok"] is False and "no attached file" in gone["error"]


def test_the_model_is_told_which_id_an_attached_file_has(scratch):
    from jarvis import uploads
    from jarvis.attachments import compose_message, prepare_for_turn

    upload = uploads.save_upload(png(), "cover.png")
    composed = compose_message("put this in content", prepare_for_turn([upload["id"]], session_id="s1"))
    assert f'"cover.png" = {upload["id"]}' in composed
    assert composed.endswith("put this in content")


def test_jarvis_edits_schedules_and_records_numbers_with_the_same_functions(scratch):
    from jarvis import assembly

    registry = assembly.get_registry()
    item = _video()
    edited = registry.get("edit_content_item").handler(content_item_id=item["id"],
                                                         fields={"caption": "Written by Jarvis"})
    assert edited["ok"] is True
    stored = store.item_detail(item["id"])
    assert stored["fields"]["caption"] == "Written by Jarvis" and stored["events"][0]["actor"] == "Jarvis"

    schedule = registry.get("schedule_content").handler
    refused = schedule(content_item_id=item["id"], platform="youtube", scheduled_at=_later(),
                       timezone="Europe/London")
    assert refused["ok"] is False and "approved" in refused["error"]
    lifecycle.approve(item["id"])
    done = schedule(content_item_id=item["id"], platform="youtube", scheduled_at="2099-01-02T09:00:00+00:00",
                    timezone="Europe/London")
    assert done["ok"] is True, done
    (placement,) = store.get_item(item["id"])["placements"]
    assert (placement["platform"], placement["status"], placement["scheduledAt"]) == \
        ("youtube", "scheduled", "2099-01-02T09:00:00.000Z")
    moved = schedule(content_item_id=item["id"], platform="youtube", scheduled_at="2099-01-03T09:00:00+00:00",
                     timezone="Europe/London")
    assert moved["ok"] is True and len(store.get_item(item["id"])["placements"]) == 1

    lifecycle.post_now(placement["id"])
    lifecycle.claim(placement["id"], by="PostBot")
    lifecycle.report_result(placement["id"], ok=True, url="https://youtube.example/1", by="PostBot")
    numbers = registry.get("record_content_metrics").handler(content_item_id=item["id"], platform="youtube",
                                                             metrics={"views": 42})
    assert numbers["ok"] is True
    assert store.get_item(item["id"])["placements"][0]["metrics"]["values"] == {"views": 42}
    assert store.get_item(item["id"])["placements"][0]["metrics"]["source"] == "Jarvis"
