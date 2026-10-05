"""Content Management's lifecycle, against the real store on a scratch database.

Every allowed move is exercised, and — just as important — every move that
must be REFUSED is asserted to be refused, with a message a person can read.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

import pytest

from jarvis.content_manager import files, lifecycle, store
from jarvis.content_manager.lifecycle import ContentError
from jarvis.db import get_db
from jarvis.events import EventType
from jarvis.events.bus import EventBus
from jarvis.jscompat import to_iso_z

from content_samples import png


@pytest.fixture(autouse=True)
def _isolated(scratch):
    yield


@pytest.fixture
def ebus():
    return EventBus()


def _later(minutes=60):
    return to_iso_z(datetime.now(timezone.utc) + timedelta(minutes=minutes))


def _file(name="clip.webm", data=b"\x1aE\xdf\xa3fake-webm-bytes"):
    return files.save_stream(io.BytesIO(data), name)


def _video(ebus, **extra):
    clip = _file()
    thumb = _file("thumb.png", png())
    return lifecycle.submit(
        name="3 Squat Mistakes Killing Your Gains", content_type="video", niche="Fitness",
        producer="ClipBot",
        fields={"title": "3 Squat Mistakes", "caption": "Stop doing these", "hashtags": ["#squat", "#gym"]},
        media=[{"fileId": clip["fileId"], "role": "primary"},
               {"fileId": thumb["fileId"], "role": "thumbnail"}],
        findings=[{"level": "warning", "text": "Hook could be stronger"}],
        event_bus=ebus, **extra)


def _post(ebus):
    return lifecycle.submit(name="Why agents need review", content_type="text_post", niche="AI",
                            producer="Writer", fields={"body": "Agents are fast. Review keeps them honest."},
                            event_bus=ebus)


def _approved_with(ebus, platform="tiktok"):
    item = _video(ebus)
    lifecycle.approve(item["id"], event_bus=ebus)
    return lifecycle.add_placement(item["id"], platform=platform, event_bus=ebus)


# --- submitting ------------------------------------------------------------------

def test_submitted_content_lands_in_review_with_its_media_and_history(ebus):
    seen = []
    ebus.subscribe(EventType.CONTENT_CHANGED, seen.append)
    notes = []
    ebus.subscribe(EventType.NOTIFICATION_CREATED, notes.append)
    item = _video(ebus)
    assert item["stage"] == "review" and item["revision"] == 1
    assert {m["role"] for m in item["media"]} == {"primary", "thumbnail"}
    assert item["media"][0]["url"].startswith("/api/content-media/cmf_")
    assert item["next"] == {"step": "Review it: approve, or ask for changes", "who": "You"}
    assert item["attention"] == ["Ready for your review"]
    assert seen and notes and "New content to review" in notes[0].payload["title"]
    detail = store.item_detail(item["id"])
    assert [e["kind"] for e in detail["events"]] == ["submitted"]
    assert detail["revisions"][0]["revision"] == 1


@pytest.mark.parametrize("kwargs, needle", [
    ({"name": "", "content_type": "video"}, "name"),
    ({"name": "x", "content_type": "hologram"}, "isn't a content type"),
    ({"name": "x", "content_type": "video", "fields": {"title": "t"}}, "needs the actual file"),
    ({"name": "x", "content_type": "carousel", "fields": {"caption": "c"}}, "at least one slide"),
    ({"name": "x", "content_type": "text_post", "fields": {"hashtags": ["#a"]}}, "needs its text"),
    ({"name": "x", "content_type": "text_post", "fields": {"body": "b", "thumbnail": "no"}}, "doesn't have"),
    ({"name": "x", "content_type": "text_post", "fields": {"body": "b"}, "platforms": ["youtube"]},
     "doesn't take"),
])
def test_bad_submissions_are_refused_in_plain_words(ebus, kwargs, needle):
    with pytest.raises(ContentError) as err:
        lifecycle.submit(event_bus=ebus, **kwargs)
    assert needle in str(err.value)


def test_a_file_belonging_to_one_item_cannot_be_claimed_by_another(ebus):
    item = _video(ebus)
    stolen = item["media"][0]["fileId"]
    with pytest.raises(ContentError, match="belongs to something else"):
        lifecycle.submit(name="thief", content_type="video", media=[{"fileId": stolen}], event_bus=ebus)


# --- review <-> changes requested ------------------------------------------------------

def test_request_changes_then_revision_returns_to_review(ebus):
    item = _video(ebus)
    request = lifecycle.request_changes(item["id"], what="Tighter hook in the first 2 seconds",
                                        why="Retention drops at 0:02", assignee="agent", event_bus=ebus)
    current = store.get_item(item["id"])
    assert current["stage"] == "changes_requested"
    assert current["next"] == {"step": "Pick up the change request and revise it", "who": "ClipBot"}
    assert [r["id"] for r in store.open_change_requests()] == [request["id"]]

    lifecycle.pick_up(request["id"], by="ClipBot", event_bus=ebus)
    assert store.get_item(item["id"])["next"]["step"] == "Working on the revision"
    with pytest.raises(ContentError, match="already working"):
        lifecycle.pick_up(request["id"], by="SomeoneElse", event_bus=ebus)

    new_clip = _file("clip-v2.webm")
    revised = lifecycle.submit_revision(
        item["id"], fields={"caption": "Fix these 3 squat mistakes today"},
        media=[{"fileId": new_clip["fileId"], "role": "primary"},
               {"fileId": item["media"][1]["fileId"], "role": "thumbnail"}],
        note="New hook", by="ClipBot", event_bus=ebus)
    assert revised["stage"] == "review" and revised["revision"] == 2
    assert revised["fields"]["caption"] == "Fix these 3 squat mistakes today"
    assert revised["fields"]["title"] == "3 Squat Mistakes", "untouched fields survive a revision"
    assert revised["attention"] == ["Revision ready to review"]
    detail = store.item_detail(item["id"])
    assert [r["revision"] for r in detail["revisions"]] == [2, 1]
    assert detail["revisions"][1]["fields"]["caption"] == "Stop doing these", "the old version is kept"
    assert detail["requests"][0]["status"] == "resolved" and detail["requests"][0]["resolvedRevision"] == 2
    assert store.open_change_requests() == []


def test_a_revision_nobody_asked_for_is_refused(ebus):
    item = _video(ebus)
    with pytest.raises(ContentError, match="Nobody asked for changes"):
        lifecycle.submit_revision(item["id"], fields={"caption": "x"}, by="ClipBot", event_bus=ebus)


def test_withdrawing_a_request_goes_back_to_review(ebus):
    item = _post(ebus)
    request = lifecycle.request_changes(item["id"], what="Shorter", event_bus=ebus)
    lifecycle.update_request(request["id"], assignee="jarvis", event_bus=ebus)
    assert store.get_item(item["id"])["openRequest"]["assignee"] == "jarvis"
    back = lifecycle.cancel_request(request["id"], event_bus=ebus)
    assert back["stage"] == "review" and back["openRequest"] is None


def test_request_changes_needs_what(ebus):
    item = _post(ebus)
    with pytest.raises(ContentError, match="what needs to change"):
        lifecycle.request_changes(item["id"], what="  ", event_bus=ebus)


# --- approval, placements, scheduling ------------------------------------------------------

def test_approve_only_from_review(ebus):
    item = _post(ebus)
    lifecycle.approve(item["id"], event_bus=ebus)
    with pytest.raises(ContentError, match="Only something in Review"):
        lifecycle.approve(item["id"], event_bus=ebus)


def test_stage_is_derived_from_placements_after_approval(ebus):
    item = _video(ebus)
    lifecycle.approve(item["id"], event_bus=ebus)
    tiktok = lifecycle.add_placement(item["id"], platform="tiktok", event_bus=ebus)
    youtube = lifecycle.add_placement(item["id"], platform="youtube", destination="Shorts", event_bus=ebus)
    assert store.get_item(item["id"])["stage"] == "approved"

    lifecycle.schedule(tiktok["id"], scheduled_at=_later(120), timezone_name="Europe/London", event_bus=ebus)
    current = store.get_item(item["id"])
    assert current["stage"] == "scheduling"
    assert current["next"]["who"] == "Publisher"

    lifecycle.schedule(tiktok["id"], scheduled_at=_later(240), timezone_name="Europe/London", event_bus=ebus)
    kinds = [e["kind"] for e in store.item_detail(item["id"])["events"]]
    assert "rescheduled" in kinds

    lifecycle.unschedule(tiktok["id"], event_bus=ebus)
    assert store.get_item(item["id"])["stage"] == "approved"

    lifecycle.post_now(youtube["id"], event_bus=ebus)
    assert store.get_item(item["id"])["stage"] == "scheduling"
    queue = store.publish_queue()
    assert [q["placementId"] for q in queue] == [youtube["id"]]
    assert queue[0]["version"] == {"title": "3 Squat Mistakes"}, "YouTube gets the fields YouTube uses"


def test_scheduling_is_refused_before_approval_and_in_the_past(ebus):
    item = _video(ebus)
    placement = lifecycle.add_placement(item["id"], platform="tiktok", event_bus=ebus)
    with pytest.raises(ContentError, match="approved"):
        lifecycle.schedule(placement["id"], scheduled_at=_later(), event_bus=ebus)
    lifecycle.approve(item["id"], event_bus=ebus)
    with pytest.raises(ContentError, match="already passed"):
        lifecycle.schedule(placement["id"], scheduled_at=_later(-5), event_bus=ebus)
    with pytest.raises(ContentError, match="date and time"):
        lifecycle.schedule(placement["id"], scheduled_at="2030-01-01T09:00", event_bus=ebus)


def test_platform_rules(ebus):
    item = _post(ebus)
    with pytest.raises(ContentError, match="doesn't take a Text Post"):
        lifecycle.add_placement(item["id"], platform="youtube", event_bus=ebus)
    lifecycle.add_placement(item["id"], platform="linkedin", event_bus=ebus)
    with pytest.raises(ContentError, match="already going"):
        lifecycle.add_placement(item["id"], platform="linkedin", event_bus=ebus)


def test_platform_version_overrides_fall_back_to_base(ebus):
    item = _video(ebus)
    tiktok = lifecycle.add_placement(item["id"], platform="tiktok", event_bus=ebus)
    lifecycle.update_placement(tiktok["id"], overrides={"caption": "TikTok-only caption"}, event_bus=ebus)
    with pytest.raises(ContentError, match="doesn't use title"):
        lifecycle.update_placement(tiktok["id"], overrides={"title": "no"}, event_bus=ebus)
    current = store.get_item(item["id"])
    merged = store.merged_version(current, current["placements"][0])
    assert merged == {"caption": "TikTok-only caption", "hashtags": ["#squat", "#gym"]}


def test_migration_32_removes_accounts_and_keeps_every_post(tmp_path):
    """An existing database from before the Accounts feature was removed: its
    accounts table and a post's account go; the post itself, its schedule and
    everything else stay exactly as they were."""
    import sqlite3

    from jarvis import db as db_module

    conn = sqlite3.connect(str(tmp_path / "old.db"), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    for version in range(1, 32):
        conn.execute("BEGIN")
        db_module._apply_migration(conn, version)
        conn.execute(f"PRAGMA user_version = {version}")
        conn.execute("COMMIT")
    conn.execute("INSERT INTO cm_accounts (id, platform, handle, created_at) VALUES "
                 "('ca_1', 'tiktok', '@fitwithvin', '2026-09-01T00:00:00.000Z')")
    conn.execute("INSERT INTO cm_items (id, name, content_type, stage, created_at, updated_at) VALUES "
                 "('ci_1', 'Old video', 'video', 'scheduling', 'x', 'x')")
    conn.execute("INSERT INTO cm_placements (id, item_id, platform, account_id, account_label, destination, "
                 "status, scheduled_at, created_at, updated_at) VALUES ('cp_1', 'ci_1', 'tiktok', 'ca_1', "
                 "'@fitwithvin', 'Main feed', 'scheduled', '2026-12-01T09:00:00.000Z', 'x', 'x')")

    db_module.migrate(conn)

    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'cm_accounts'").fetchone()[0] == 0
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(cm_placements)")}
    assert not {"account_id", "account_label"} & columns
    assert {"media_json", "metrics_json"} <= columns
    post = conn.execute("SELECT * FROM cm_placements WHERE id = 'cp_1'").fetchone()
    assert (post["platform"], post["destination"], post["status"], post["scheduled_at"], post["media_json"]) == \
        ("tiktok", "Main feed", "scheduled", "2026-12-01T09:00:00.000Z", "[]")
    conn.close()


def test_there_is_no_account_anywhere_in_content_management(ebus):
    item = _video(ebus)
    lifecycle.approve(item["id"], event_bus=ebus)
    placed = lifecycle.add_placement(item["id"], platform="tiktok", event_bus=ebus)
    assert not [k for k in placed if "account" in k.lower()]
    with pytest.raises(TypeError):
        lifecycle.add_placement(item["id"], platform="youtube", account_id="ca_1", event_bus=ebus)
    lifecycle.post_now(placed["id"], event_bus=ebus)
    (job,) = store.publish_queue()
    assert "account" not in job
    assert not hasattr(lifecycle, "create_account") and not hasattr(store, "list_accounts")


# --- publishing -------------------------------------------------------------------------

def test_claim_is_exclusive_and_result_publishes(ebus):
    placement = _approved_with(ebus)
    lifecycle.post_now(placement["id"], event_bus=ebus)
    claimed = lifecycle.claim(placement["id"], by="PostBot", event_bus=ebus)
    assert claimed["status"] == "publishing"
    with pytest.raises(ContentError, match="isn't waiting"):
        lifecycle.claim(placement["id"], by="OtherBot", event_bus=ebus)
    assert store.publish_queue() == []
    done = lifecycle.report_result(placement["id"], ok=True, url="https://tiktok.com/@fitwithvin/video/1",
                                   by="PostBot", event_bus=ebus)
    assert done["status"] == "published" and done["publishedUrl"].endswith("/video/1")
    assert store.get_item(placement["itemId"])["stage"] == "published"


def test_a_scheduled_post_joins_the_queue_only_once_due(ebus):
    placement = _approved_with(ebus)
    lifecycle.schedule(placement["id"], scheduled_at=_later(30), event_bus=ebus)
    assert store.publish_queue() == []
    with pytest.raises(ContentError):
        lifecycle.claim(placement["id"], by="PostBot", event_bus=ebus)
    get_db().execute("UPDATE cm_placements SET scheduled_at = ? WHERE id = ?", (_later(-1), placement["id"]))
    assert [q["placementId"] for q in store.publish_queue()] == [placement["id"]]
    assert store.get_item(placement["itemId"])["placements"][0]["due"] is True
    lifecycle.claim(placement["id"], by="PostBot", event_bus=ebus)


def test_a_failed_post_needs_attention_and_can_be_retried(ebus):
    notes = []
    ebus.subscribe(EventType.NOTIFICATION_CREATED, notes.append)
    placement = _approved_with(ebus)
    lifecycle.post_now(placement["id"], event_bus=ebus)
    lifecycle.claim(placement["id"], by="PostBot", event_bus=ebus)
    lifecycle.report_result(placement["id"], ok=False, error="Token expired", by="PostBot", event_bus=ebus)
    item = store.get_item(placement["itemId"])
    assert item["stage"] == "approved" and item["attention"] == ["Post failed on TikTok"]
    assert store.summary()["attention"]["failedPosts"] == 1
    assert any("Post failed" in n.payload["title"] for n in notes)
    lifecycle.requeue(placement["id"], event_bus=ebus)
    assert store.get_item(placement["itemId"])["stage"] == "scheduling"


def test_i_posted_it_myself(ebus):
    placement = _approved_with(ebus)
    lifecycle.mark_posted(placement["id"], url="https://tiktok.com/x", event_bus=ebus)
    assert store.get_item(placement["itemId"])["stage"] == "published"
    with pytest.raises(ContentError, match="already published"):
        lifecycle.request_changes(placement["itemId"], what="x", event_bus=ebus)


# --- archive and recycle bin ------------------------------------------------------------------

def test_archive_cancels_schedules_and_unarchive_returns(ebus):
    placement = _approved_with(ebus)
    lifecycle.schedule(placement["id"], scheduled_at=_later(), event_bus=ebus)
    archived = lifecycle.archive(placement["itemId"], event_bus=ebus)
    assert archived["stage"] == "archived" and archived["archivedFrom"] == "scheduling"
    assert archived["placements"][0]["status"] == "draft", "an archived item never goes out"
    assert store.list_items(stage="scheduling") == []
    assert [i["id"] for i in store.list_items(stage="archived", niche="fitness")] == [placement["itemId"]]
    back = lifecycle.unarchive(placement["itemId"], event_bus=ebus)
    assert back["stage"] == "approved", "nothing is scheduled any more, so it is Ready to Post"


def test_nothing_mid_publish_can_be_archived_or_deleted(ebus):
    placement = _approved_with(ebus)
    lifecycle.post_now(placement["id"], event_bus=ebus)
    lifecycle.claim(placement["id"], by="PostBot", event_bus=ebus)
    for move in (lifecycle.archive, lifecycle.delete):
        with pytest.raises(ContentError, match="being published right now"):
            move(placement["itemId"], event_bus=ebus)


def test_recycle_bin_restore_and_delete_forever_removes_rows_and_files(ebus, scratch):
    item = _video(ebus)
    lifecycle.request_changes(item["id"], what="Louder audio", event_bus=ebus)
    paths = files.paths_for_item(item["id"])
    assert len(paths) == 2 and all(p.is_file() for p in paths)

    with pytest.raises(ContentError, match="Only something in the Recycle Bin"):
        lifecycle.purge(item["id"], event_bus=ebus)
    lifecycle.delete(item["id"], event_bus=ebus)
    assert store.list_items(stage="changes_requested") == []
    assert store.open_change_requests() == [], "an agent never works on something in the bin"
    assert [i["id"] for i in store.list_items(stage="bin")] == [item["id"]]
    with pytest.raises(ContentError, match="Recycle Bin"):
        lifecycle.approve(item["id"], event_bus=ebus)

    restored = lifecycle.restore(item["id"], event_bus=ebus)
    assert restored["stage"] == "changes_requested" and restored["openRequest"]["what"] == "Louder audio"

    lifecycle.delete(item["id"], event_bus=ebus)
    lifecycle.purge(item["id"], event_bus=ebus)
    assert store.get_item(item["id"]) is None
    assert not any(p.exists() for p in paths)
    db = get_db()
    for table in ("cm_placements", "cm_change_requests", "cm_revisions", "cm_events", "cm_files"):
        assert db.execute(f"SELECT COUNT(*) FROM {table} WHERE item_id = ?", (item["id"],)).fetchone()[0] == 0


def test_empty_bin_counts(ebus):
    for _ in range(3):
        lifecycle.delete(_post(ebus)["id"], event_bus=ebus)
    assert store.summary()["counts"]["bin"] == 3
    assert lifecycle.empty_bin(event_bus=ebus) == 3
    assert store.summary()["counts"]["bin"] == 0


# --- reading ---------------------------------------------------------------------------------

def test_filters_and_summary_counts_follow_them(ebus):
    _video(ebus)
    _post(ebus)
    carousel_slides = [_file(f"s{i}.png", png(rgb=(i * 60, 80, 90))) for i in range(3)]
    lifecycle.submit(name="5 Money Rules", content_type="carousel", niche="Finance",
                     fields={"caption": "Save this"},
                     media=[{"fileId": s["fileId"], "role": "slide", "order": i}
                            for i, s in enumerate(carousel_slides)], event_bus=ebus)
    assert store.summary()["counts"]["review"] == 3
    assert store.summary(niche="Finance")["counts"]["review"] == 1
    assert [i["name"] for i in store.list_items(stage="review", content_type="carousel")] == ["5 Money Rules"]
    assert [i["name"] for i in store.list_items(stage="review", q="honest")] == ["Why agents need review"]
    slides = store.list_items(content_type="carousel")[0]["media"]
    assert [m["order"] for m in slides] == [0, 1, 2]
    assert store.niches() == ["AI", "Finance", "Fitness"]


def test_calendar_lists_scheduled_and_published_posts(ebus):
    placement = _approved_with(ebus)
    when = _later(60 * 24)
    lifecycle.schedule(placement["id"], scheduled_at=when, timezone_name="Africa/Lagos", event_bus=ebus)
    start = to_iso_z(datetime.now(timezone.utc))
    end = to_iso_z(datetime.now(timezone.utc) + timedelta(days=3))
    entries = store.calendar(start=start, end=end)
    assert [(e["id"], e["itemName"], e["timezone"]) for e in entries] == [
        (placement["id"], "3 Squat Mistakes Killing Your Gains", "Africa/Lagos")]


def test_content_stays_editable_until_it_has_gone_out(ebus):
    item = _post(ebus)
    lifecycle.edit(item["id"], fields={"body": "Edited body"}, niche="AI Tools", event_bus=ebus)
    assert store.get_item(item["id"])["fields"]["body"] == "Edited body"
    lifecycle.request_changes(item["id"], what="x", event_bus=ebus)
    assert store.get_item(item["id"])["editable"] is False
    with pytest.raises(ContentError, match="from Review until it has been published"):
        lifecycle.edit(item["id"], fields={"body": "nope"}, event_bus=ebus)
    lifecycle.cancel_request(store.get_item(item["id"])["openRequest"]["id"], event_bus=ebus)
    with pytest.raises(ContentError, match="needs its text"):
        lifecycle.edit(item["id"], fields={"body": ""}, event_bus=ebus)

    # Scheduled is still a plan: the change goes out with it.
    lifecycle.approve(item["id"], event_bus=ebus)
    x = lifecycle.add_placement(item["id"], platform="x", event_bus=ebus)
    li = lifecycle.add_placement(item["id"], platform="linkedin", event_bus=ebus)
    lifecycle.schedule(x["id"], scheduled_at=_later(), timezone_name="Europe/London", event_bus=ebus)
    assert store.get_item(item["id"])["stage"] == "scheduling"
    lifecycle.edit(item["id"], fields={"body": "Changed while scheduled"}, event_bus=ebus)
    assert store.get_item(item["id"])["revision"] == 1

    # Not while a post is on its way — the publisher may already hold the old text.
    lifecycle.post_now(li["id"], event_bus=ebus)
    with pytest.raises(ContentError, match="on its way"):
        lifecycle.edit(item["id"], fields={"body": "nope"}, event_bus=ebus)

    # Once one platform has published, a change is a NEW revision: what went out stays on record.
    lifecycle.claim(li["id"], by="PostBot", event_bus=ebus)
    lifecycle.report_result(li["id"], ok=True, url="https://linkedin.example/1", event_bus=ebus)
    lifecycle.edit(item["id"], fields={"body": "For the X post only now"}, event_bus=ebus)
    detail = store.item_detail(item["id"])
    assert detail["revision"] == 2
    assert [r["fields"]["body"] for r in detail["revisions"]] == ["For the X post only now",
                                                                  "Changed while scheduled"]

    # Everything out: nothing left to change.
    lifecycle.post_now(x["id"], event_bus=ebus)
    lifecycle.claim(x["id"], by="PostBot", event_bus=ebus)
    lifecycle.report_result(x["id"], ok=True, url="https://x.example/1", event_bus=ebus)
    assert store.get_item(item["id"])["editable"] is False
    with pytest.raises(ContentError, match="until it has been published"):
        lifecycle.edit(item["id"], fields={"body": "too late"}, event_bus=ebus)


def test_a_revision_on_record_includes_the_persons_own_edits(ebus):
    item = _post(ebus)
    lifecycle.edit(item["id"], fields={"body": "Edited by the owner"}, event_bus=ebus)
    lifecycle.request_changes(item["id"], what="Shorter", event_bus=ebus)
    lifecycle.submit_revision(item["id"], fields={"body": "Short"}, by="Writer", event_bus=ebus)
    revisions = store.item_detail(item["id"])["revisions"]
    assert [r["fields"]["body"] for r in revisions] == ["Short", "Edited by the owner"]
