"""Niches are folders: a niche can exist empty, be renamed, and be deleted only
when nothing is in it. Every piece of content is still its own item in the one
pipeline — the folder is the niche NAME it carries.

Every assertion checks what the backend stored, not what a response claimed.
"""

from __future__ import annotations

import io
import random
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import pytest
from starlette.testclient import TestClient

from jarvis.content_manager import files, lifecycle, store
from jarvis.content_manager.lifecycle import ContentError, NotFound
from jarvis.db import get_db
from jarvis.events import EventBus, EventType
from jarvis.jscompat import to_iso_z

from content_samples import png

WEBM = b"\x1aE\xdf\xa3" + b"\x00" * 64


@pytest.fixture(autouse=True)
def _isolated(scratch):
    """EVERY test here runs on a scratch data dir."""
    from jarvis import assembly

    assembly.reset_for_tests()
    yield
    assembly.reset_for_tests()


@pytest.fixture
def client(scratch):
    from jarvis.main import create_app

    return TestClient(create_app())


def _file(name="clip.webm", data=WEBM):
    return files.save_stream(io.BytesIO(data), name)["fileId"]


def _add(name, niche="", content_type="video", producer="ClipBot", **extra):
    if content_type in ("video", "image", "flyer", "audio"):
        media = [{"fileId": _file(), "role": "primary"}]
        fields = {"title": name}
    elif content_type == "carousel":
        media = [{"fileId": _file("a.png", png()), "role": "slide"}, {"fileId": _file("b.png", png()), "role": "slide"}]
        fields = {"caption": name}
    else:
        from jarvis.content_manager.kinds import TYPES

        media = []
        text = {"body": f"{name} body", "subject": name, "title": name}
        fields = {k: v for k, v in text.items() if k in TYPES[content_type]["fields"]}
    return lifecycle.submit(name=name, content_type=content_type, niche=niche, producer=producer,
                            fields=fields, media=media, **extra)


def _niche_rows():
    return {r["name"] for r in get_db().execute("SELECT name FROM cm_niches")}


def _item_niche(item_id):
    return get_db().execute("SELECT niche FROM cm_items WHERE id = ?", (item_id,)).fetchone()[0]


# --- a niche is a folder ------------------------------------------------------------------------

def test_a_niche_can_exist_empty_and_is_listed_as_a_folder():
    assert lifecycle.create_niche("  Psychology  ") == "Psychology"
    assert _niche_rows() == {"Psychology"}
    overview = store.niche_overview()
    [folder] = overview["niches"]
    assert folder["name"] == "Psychology" and folder["total"] == 0 and folder["byType"] == {}
    assert store.niches() == ["Psychology"]


def test_creating_a_niche_that_exists_in_any_spelling_is_refused_and_names_need_words():
    lifecycle.create_niche("Psychology")
    with pytest.raises(ContentError, match="already a niche called “Psychology”"):
        lifecycle.create_niche("psychology")
    with pytest.raises(ContentError, match="name"):
        lifecycle.create_niche("   ")
    assert lifecycle.create_niche("Personal   Finance") == "Personal Finance"
    assert len(lifecycle.create_niche("x" * 200)) == 80


def test_handing_in_with_a_new_niche_makes_the_folder_and_a_known_one_keeps_its_spelling():
    lifecycle.create_niche("Psychology")
    agent = _add("Dopamine explained", niche="psychology")
    assert _item_niche(agent["id"]) == "Psychology"          # into the folder, not beside it
    jarvis = _add("Stoic mornings", niche="Stoicism", producer="Jarvis", content_type="text_post")
    assert _item_niche(jarvis["id"]) == "Stoicism"
    assert _niche_rows() == {"Psychology", "Stoicism"}
    none = _add("Untitled clip")
    assert _item_niche(none["id"]) == ""
    assert _niche_rows() == {"Psychology", "Stoicism"}    # no niche is not a folder


def test_moving_an_item_between_niches_is_an_edit_and_is_on_its_history():
    lifecycle.create_niche("Psychology")
    item = _add("Dopamine explained", niche="Psychology")
    lifecycle.edit(item["id"], niche="fitness")
    assert _item_niche(item["id"]) == "fitness" and "fitness" in _niche_rows()
    lifecycle.edit(item["id"], niche="FITNESS")                  # the same folder: nothing moves
    lifecycle.edit(item["id"], niche="")
    assert _item_niche(item["id"]) == ""
    notes = [e["note"] for e in store.item_detail(item["id"])["events"] if e["kind"] == "moved"]
    assert notes == ["Taken out of “fitness”", "Moved to “fitness”"]
    # The folder it left stays: niches are only ever deleted on purpose.
    assert {"Psychology", "fitness"} <= _niche_rows()


def test_rename_moves_everything_in_the_folder_even_the_bin_and_touches_no_schedule():
    lifecycle.create_niche("Psych")
    kept = [_add(f"Clip {n}", niche="Psych") for n in range(3)]
    binned = _add("Old clip", niche="Psych")
    lifecycle.delete(binned["id"])
    lifecycle.approve(kept[0]["id"])
    placement = lifecycle.add_placement(kept[0]["id"], platform="tiktok")
    when = to_iso_z(datetime.now(timezone.utc) + timedelta(days=2))
    lifecycle.schedule(placement["id"], scheduled_at=when)
    other = _add("Leg day", niche="Fitness")
    before = get_db().execute("SELECT scheduled_at, status FROM cm_placements WHERE id = ?",
                              (placement["id"],)).fetchone()

    assert lifecycle.rename_niche("psych", "Psychology") == "Psychology"

    assert _niche_rows() == {"Psychology", "Fitness"}
    for item in [*kept, binned]:
        assert _item_niche(item["id"]) == "Psychology"
    assert _item_niche(other["id"]) == "Fitness"
    after = get_db().execute("SELECT scheduled_at, status FROM cm_placements WHERE id = ?",
                             (placement["id"],)).fetchone()
    assert tuple(after) == tuple(before)
    assert store.item_detail(kept[1]["id"])["events"][0]["note"] == "Niche renamed from “Psych” to “Psychology”"
    # Capitals only is a rename too.
    assert lifecycle.rename_niche("Psychology", "PSYCHOLOGY") == "PSYCHOLOGY"
    assert _item_niche(kept[2]["id"]) == "PSYCHOLOGY"


def test_rename_onto_another_existing_niche_is_refused_and_changes_nothing():
    lifecycle.create_niche("Psychology")
    a = _add("A", niche="Psychology")
    b = _add("B", niche="Fitness")
    with pytest.raises(ContentError, match="never merged"):
        lifecycle.rename_niche("Psychology", "fitness")
    assert _item_niche(a["id"]) == "Psychology" and _item_niche(b["id"]) == "Fitness"
    assert _niche_rows() == {"Psychology", "Fitness"}
    with pytest.raises(NotFound):
        lifecycle.rename_niche("Nope", "Other")
    with pytest.raises(ContentError):
        lifecycle.rename_niche("Psychology", "  ")


def test_a_niche_that_holds_anything_even_only_in_the_bin_cannot_be_deleted():
    lifecycle.create_niche("Psychology")
    items = [_add(f"Clip {n}", niche="Psychology") for n in range(3)]
    with pytest.raises(ContentError, match="still holds 3 items"):
        lifecycle.delete_niche("Psychology")
    lifecycle.archive(items[0]["id"])
    for item in items[1:]:
        lifecycle.delete(item["id"])
    with pytest.raises(ContentError, match=r"3 items \(2 of them in the Recycle Bin\)"):
        lifecycle.delete_niche("psychology")
    lifecycle.edit(items[0]["id"], niche="Elsewhere")
    with pytest.raises(ContentError, match="2 items in the Recycle Bin"):
        lifecycle.delete_niche("Psychology")
    for item in items[1:]:
        lifecycle.purge(item["id"])
    lifecycle.delete_niche("Psychology")
    assert _niche_rows() == {"Elsewhere"}
    # Nothing was deleted along with the folder.
    assert get_db().execute("SELECT COUNT(*) FROM cm_items").fetchone()[0] == 1
    with pytest.raises(NotFound):
        lifecycle.delete_niche("Psychology")


def test_folder_changes_are_announced_so_an_open_screen_refreshes():
    bus = EventBus()
    seen = []
    bus.subscribe(EventType.CONTENT_CHANGED, seen.append)
    lifecycle.create_niche("Psychology", event_bus=bus)
    lifecycle.rename_niche("Psychology", "Psych", event_bus=bus)
    lifecycle.delete_niche("Psych", event_bus=bus)
    assert len(seen) == 3


def test_your_own_additions_do_not_notify_you_but_everyone_elses_still_do():
    bus = EventBus()
    notes = []
    bus.subscribe(EventType.NOTIFICATION_CREATED, notes.append)
    for n in range(10):
        _add(f"My clip {n}", niche="Psychology", producer="you", event_bus=bus)
    assert notes == []
    _add("Agent clip", niche="Psychology", producer="ClipBot", event_bus=bus)
    _add("Jarvis post", niche="Psychology", producer="Jarvis", content_type="text_post", event_bus=bus)
    assert [n.payload["title"] for n in notes] == ["New content to review: Agent clip",
                                          "New content to review: Jarvis post"]


# --- reading a folder ----------------------------------------------------------------------------

def test_no_niche_and_all_filter_every_reading_the_same_way():
    lifecycle.create_niche("Empty")
    psych = [_add(f"P{n}", niche="Psychology") for n in range(3)]
    loose = [_add(f"L{n}") for n in range(2)]
    lifecycle.archive(psych[0]["id"])
    lifecycle.approve(loose[0]["id"])
    placement = lifecycle.add_placement(loose[0]["id"], platform="tiktok")
    lifecycle.mark_posted(placement["id"], url="https://example.com/x")
    lifecycle.record_metrics(placement["id"], metrics={"views": 10})

    names = lambda **q: sorted(i["name"] for i in store.list_items(**q))  # noqa: E731
    assert names(stage="active", no_niche=True) == ["L0", "L1"]
    assert names(stage="active", niche="psychology") == ["P1", "P2"]           # not the archived one
    assert names(stage="archived", niche="Psychology") == ["P0"]
    assert names(stage="active", niche="Empty") == []
    assert store.count_items(stage="active", no_niche=True) == 2
    counts = store.summary(no_niche=True)["counts"]
    assert counts["active"] == 2 and counts["review"] == 1 and counts["published"] == 1
    assert store.summary(niche="Psychology")["counts"]["active"] == 2
    assert [p["name"] for p in store.analytics(no_niche=True)["posts"]] == ["L0"]
    assert store.analytics(niche="Psychology")["posts"] == []
    start = to_iso_z(datetime.now(timezone.utc) - timedelta(days=1))
    end = to_iso_z(datetime.now(timezone.utc) + timedelta(days=1))
    assert [e["itemName"] for e in store.calendar(start=start, end=end, no_niche=True)] == ["L0"]
    assert store.calendar(start=start, end=end, niche="Psychology") == []


def test_the_overview_agrees_with_an_independent_recount_of_a_busy_library():
    rng = random.Random(33)
    niches = ["Psychology", "Fitness", "Personal Finance", "Cooking & Baking", "Travel", ""]
    kinds = ["video"] * 5 + ["image", "carousel", "article", "text_post", "flyer", "audio", "newsletter"]
    lifecycle.create_niche("Empty one")
    made = []
    for n in range(160):
        made.append(_add(f"Piece {n}", niche=rng.choice(niches), content_type=rng.choice(kinds)))
    for item in rng.sample(made, 40):
        lifecycle.approve(item["id"])
    for item in rng.sample(made, 15):
        lifecycle.archive(item["id"])
    for item in rng.sample(made, 15):
        lifecycle.delete(item["id"])

    rows = get_db().execute("SELECT niche, content_type, stage, deleted_at FROM cm_items").fetchall()
    expected = defaultdict(lambda: {"total": 0, "byType": Counter(), "review": 0, "archived": 0, "binned": 0})
    for r in rows:
        for key in (r["niche"], "*"):
            f = expected[key]
            if r["deleted_at"]:
                f["binned"] += 1
            elif r["stage"] == "archived":
                f["archived"] += 1
            else:
                f["total"] += 1
                f["byType"][r["content_type"]] += 1
                f["review"] += r["stage"] == "review"

    overview = store.niche_overview()

    def check(folder, want):
        assert folder["total"] == want["total"]
        assert folder["byType"] == dict(want["byType"])
        assert (folder["review"], folder["archived"], folder["binned"]) == (
            want["review"], want["archived"], want["binned"])

    assert [f["name"] for f in overview["niches"]] == sorted(
        [n for n in niches if n] + ["Empty one"], key=str.lower)
    for folder in overview["niches"]:
        check(folder, expected[folder["name"]])
        # And the folder's own list says the same as its card.
        assert store.count_items(stage="active", niche=folder["name"]) == folder["total"]
    check(overview["none"], expected[""])
    check(overview["all"], expected["*"])
    assert sum(f["total"] for f in overview["niches"]) + overview["none"]["total"] == overview["all"]["total"]
    assert store.count_items(stage="active", no_niche=True) == overview["none"]["total"]


# --- migration 33 ------------------------------------------------------------------------------

def test_migration_33_turns_existing_labels_into_folders_one_per_niche(tmp_path):
    from jarvis import db as db_module

    conn = sqlite3.connect(str(tmp_path / "old.db"), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    for version in range(1, 33):
        conn.execute("BEGIN")
        db_module._apply_migration(conn, version)
        conn.execute(f"PRAGMA user_version = {version}")
        conn.execute("COMMIT")
    rows = [("ci_1", "psychology", "2026-05-01"), ("ci_2", "Psychology", "2026-06-01"),
            ("ci_3", "Psychology", "2026-07-01"), ("ci_4", "Fitness", "2026-04-01"), ("ci_5", "", "2026-01-01")]
    for item_id, niche, stamp in rows:
        conn.execute("INSERT INTO cm_items (id, name, content_type, niche, stage, created_at, updated_at) "
                     "VALUES (?, 'x', 'video', ?, 'review', ?, ?)", (item_id, niche, stamp, stamp))

    db_module.migrate(conn)

    niches = {r["name"]: r["created_at"] for r in conn.execute("SELECT * FROM cm_niches")}
    assert niches == {"Psychology": "2026-06-01", "Fitness": "2026-04-01"}
    stored = {r["id"]: r["niche"] for r in conn.execute("SELECT id, niche FROM cm_items")}
    assert stored == {"ci_1": "Psychology", "ci_2": "Psychology", "ci_3": "Psychology", "ci_4": "Fitness",
                      "ci_5": ""}


# --- over HTTP ---------------------------------------------------------------------------------

def test_the_folder_routes_including_names_with_spaces_ampersands_and_slashes(client):
    for name in ("Health / Fitness", "Cooking & Baking", "Psychology"):
        response = client.post("/api/content-niches", json={"name": name})
        assert response.status_code == 200 and response.json()["name"] == name
    assert client.post("/api/content-niches", json={"name": "psychology"}).status_code == 400
    assert client.get("/api/content-meta").json()["niches"] == ["Cooking & Baking", "Health / Fitness",
                                                                  "Psychology"]
    _add("Squats", niche="Health / Fitness")

    renamed = client.patch(f"/api/content-niches/{quote('Health / Fitness', safe='')}",
                           json={"name": "Health & Fitness / Gym"})
    assert renamed.status_code == 200, renamed.text
    assert _niche_rows() == {"Cooking & Baking", "Health & Fitness / Gym", "Psychology"}

    refused = client.delete(f"/api/content-niches/{quote('Health & Fitness / Gym', safe='')}")
    assert refused.status_code == 400 and "still holds 1 item" in refused.json()["error"]
    assert client.delete(f"/api/content-niches/{quote('Cooking & Baking', safe='')}").status_code == 200
    assert client.delete("/api/content-niches/Nope").status_code == 404

    overview = client.get("/api/content-niches").json()
    assert [(f["name"], f["total"]) for f in overview["niches"]] == [("Health & Fitness / Gym", 1),
                                                                      ("Psychology", 0)]
    _add("Loose clip")
    listed = client.get("/api/content-items", params={"stage": "active", "noNiche": "1", "limit": 10}).json()
    assert [i["name"] for i in listed["items"]] == ["Loose clip"] and listed["total"] == 1
    summary = client.get("/api/content-items/summary", params={"niche": "Psychology"}).json()
    assert summary["counts"]["active"] == 0


def test_an_agent_handing_in_over_http_with_a_new_niche_makes_its_folder(client):
    response = client.post("/api/content-items", json={
        "name": "Why we procrastinate", "contentType": "article", "niche": "  Psychology ",
        "producer": "Writer", "fields": {"title": "Why we procrastinate", "body": "Because."}})
    assert response.status_code == 200, response.text
    assert _niche_rows() == {"Psychology"}
    assert response.json()["item"]["niche"] == "Psychology"
