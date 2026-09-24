"""Content Management when it is actually busy.

~1,500 items across ten niches — every format, one to five platforms each,
every stage, schedules in six timezones, rounds of revisions, published and
failed posts, archive and the bin — built through the real lifecycle
(`content_seed`), never raw SQL. Then:

- every count the screen shows is checked against an INDEPENDENT recount made
  in Python from the raw rows (not the store's own SQL);
- every filter and search returns exactly the right items, and paging through a
  busy stage sees each item once;
- the busiest reads stay quick;
- and all of it is identical after a real restart of a real `jarvis.main`.

The concurrency half lives in `test_content_concurrency.py`.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import content_seed
from jarvis.content_manager import store
from jarvis.db import get_db
from jarvis.db import reset_for_tests as reset_db

COUNT = 1500
POST_APPROVAL = {"approved", "scheduling", "published"}
VIEW = {"approved": {"draft", "failed"}, "scheduling": {"scheduled", "queued", "publishing"},
        "published": {"published"}}


@pytest.fixture(scope="module")
def busy(tmp_path_factory):
    """One busy data dir for the whole module (seeding takes a while)."""
    import os

    root = tmp_path_factory.mktemp("busy")
    data = root / "data"
    data.mkdir()
    old = {k: os.environ.get(k) for k in ("JARVIS_DATA_DIR", "JARVIS_ENV_PATH")}
    os.environ["JARVIS_DATA_DIR"] = str(data)
    os.environ["JARVIS_ENV_PATH"] = str(root / ".env")
    reset_db()
    from jarvis.store import data_dir
    assert str(data_dir()).startswith(str(root)), "refusing to seed anything but a scratch data dir"
    started = time.monotonic()
    seeded = content_seed.seed(COUNT)
    seconds = time.monotonic() - started
    yield type("Busy", (), {"root": root, "data": data, "seeded": seeded, "seconds": seconds})()
    reset_db()
    for key, value in old.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _raw():
    """Every item with its placements' statuses, read straight from the tables."""
    db = get_db()
    items = {r["id"]: dict(r) for r in db.execute("SELECT * FROM cm_items")}
    statuses: dict[str, list[str]] = {i: [] for i in items}
    for r in db.execute("SELECT item_id, status FROM cm_placements"):
        statuses[r["item_id"]].append(r["status"])
    return items, statuses


def _expected_views(items, statuses):
    """Which views each item belongs in — worked out here, independently."""
    views: dict[str, set[str]] = {}
    for item_id, row in items.items():
        if row["deleted_at"]:
            views[item_id] = {"bin"}
            continue
        stage = row["stage"]
        if stage not in POST_APPROVAL:
            views[item_id] = {stage}
            continue
        mine = set(statuses[item_id])
        found = {view for view, wanted in VIEW.items() if mine & wanted}
        if not statuses[item_id]:
            found.add("approved")
        views[item_id] = found
    return views


def test_the_seed_is_realistic(busy):
    items, statuses = _raw()
    assert len(items) >= COUNT
    assert len({r["niche"] for r in items.values()}) >= 10
    assert set(Counter(r["content_type"] for r in items.values())) >= {
        "video", "image", "carousel", "article", "text_post", "audio", "newsletter", "flyer"}
    placements = Counter(s for ss in statuses.values() for s in ss)
    assert placements["published"] > 300 and placements["scheduled"] > 300 and placements["failed"] > 30
    zones = {r[0] for r in get_db().execute("SELECT DISTINCT timezone FROM cm_placements WHERE timezone != ''")}
    assert len(zones) == 6
    revised = get_db().execute("SELECT COUNT(DISTINCT item_id) FROM cm_revisions WHERE revision > 1").fetchone()[0]
    assert revised > 200
    multi = sum(1 for ss in statuses.values() if len(set(ss) & {"scheduled", "published", "draft", "failed"}) >= 2)
    assert multi > 100, "plenty of items have platforms in different states"


def test_every_count_matches_an_independent_recount(busy):
    items, statuses = _raw()
    expected = _expected_views(items, statuses)
    summary = store.summary()
    for view in ("review", "changes_requested", "approved", "scheduling", "published", "archived", "bin"):
        want = {i for i, v in expected.items() if view in v}
        assert summary["counts"][view] == len(want), view
        assert store.count_items(stage=view) == len(want), view
        got = {i["id"] for i in store.list_items(stage=view)}
        assert got == want, view
    live_post_approval = {i for i, r in items.items() if not r["deleted_at"] and r["stage"] in POST_APPROVAL}
    for view, wanted in VIEW.items():
        n = sum(1 for i in live_post_approval for s in statuses[i] if s in wanted)
        assert summary["posts"][view] == n, view
    # An item live on one platform and waiting on another is in BOTH lists.
    both = [i for i, v in expected.items() if {"scheduling", "published"} <= v]
    assert both, "the seed has items published somewhere and scheduled elsewhere"


def test_filters_and_search_return_exactly_the_right_items(busy):
    items, _ = _raw()
    live = {i: r for i, r in items.items() if not r["deleted_at"]}
    for niche in content_seed.NICHES:
        want = {i for i, r in live.items() if r["niche"] == niche and r["stage"] == "review"}
        assert {i["id"] for i in store.list_items(stage="review", niche=niche)} == want, niche
    for word in ("Lisbon", "Jollof", "#fitness", "save this for later", "title", "caption"):
        needle = word.lower()
        want = set()
        for i, r in live.items():
            values = [r["name"], r["niche"], r["producer"]]
            for value in json.loads(r["fields_json"]).values():
                values += value if isinstance(value, list) else [value]
            if any(needle in str(v).lower() for v in values):
                want.add(i)
        got = set()
        for stage in ("review", "changes_requested", "approved", "scheduling", "published", "archived"):
            got |= {x["id"] for x in store.list_items(stage=stage, q=word)}
        assert got == want, word
    # A field NAME is not content: only items whose own words say it.
    assert len(set().union(*[{x["id"] for x in store.list_items(stage=s, q="title")}
                             for s in ("review", "published")])) < 20


def test_paging_through_the_busiest_stage_sees_every_item_once(busy):
    total = store.count_items(stage="scheduling")
    assert total > 300
    seen: list[str] = []
    offset = 0
    while True:
        page = store.list_items(stage="scheduling", limit=50, offset=offset)
        if not page:
            break
        seen += [i["id"] for i in page]
        offset += 50
    assert len(seen) == total and len(set(seen)) == total


def test_the_calendar_on_a_busy_month(busy):
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=31)
    s, e = start.strftime("%Y-%m-%dT%H:%M:%S.000Z"), end.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    entries = store.calendar(start=s, end=e)
    want = get_db().execute(
        "SELECT COUNT(*) FROM cm_placements p JOIN cm_items i ON i.id = p.item_id WHERE i.deleted_at IS NULL "
        "AND i.stage != 'archived' AND p.status != 'draft' AND COALESCE(p.published_at, p.scheduled_at) >= ? "
        "AND COALESCE(p.published_at, p.scheduled_at) < ?", (s, e)).fetchone()[0]
    assert len(entries) == want > 100


def test_the_busiest_reads_stay_quick(busy):
    def timed(fn):
        best = min(_elapsed(fn) for _ in range(3))
        return best

    limits = {
        "a page of the busiest stage": (lambda: store.list_items(stage="scheduling", limit=50), 0.5),
        "the stage counts": (store.summary, 0.5),
        "a search across everything": (lambda: store.list_items(stage="published", q="rain"), 1.0),
        "one item, whole": (lambda: store.item_detail(busy.seeded.items[0]), 0.1),
        "analytics": (store.analytics, 1.0),
    }
    measured = {name: timed(fn) for name, (fn, _) in limits.items()}
    print("\n" + json.dumps({k: round(v * 1000) for k, v in measured.items()}, indent=1))
    for name, (_, limit) in limits.items():
        assert measured[name] < limit, f"{name} took {measured[name]:.2f}s"


def _elapsed(fn) -> float:
    started = time.perf_counter()
    fn()
    return time.perf_counter() - started


def test_everything_is_identical_after_a_real_restart(busy):
    from test_content_e2e import _start_jarvis, _stop

    before = store.summary()
    sample = {i: store.item_detail(i) for i in busy.seeded.items[::97]}
    reset_db()  # this process lets go of the database before a real server opens it
    for _ in range(2):
        proc, base = _start_jarvis(busy.data, busy.root / ".env")
        try:
            h = httpx.Client(base_url=base, timeout=60)
            assert h.get("/api/content-items/summary").json() == before
            for item_id, old in sample.items():
                now = h.get(f"/api/content-items/{item_id}").json()["item"]
                for key in ("stage", "revision", "fields", "niche", "deletedAt", "archivedFrom"):
                    assert now[key] == old[key], (item_id, key)
                assert [(p["platform"], p["status"], p["scheduledAt"], p["timezone"], p["publishedUrl"])
                        for p in now["placements"]] == \
                       [(p["platform"], p["status"], p["scheduledAt"], p["timezone"], p["publishedUrl"])
                        for p in old["placements"]]
                assert len(now["events"]) == len(old["events"])
                assert all(h.get(m["url"]).status_code == 200 for m in now["media"])
            page = h.get("/api/content-items?stage=scheduling&limit=50").json()
            assert page["total"] == before["counts"]["scheduling"] and len(page["items"]) == 50
        finally:
            _stop(proc)
