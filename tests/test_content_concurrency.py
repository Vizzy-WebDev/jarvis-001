"""Content Management with many hands on it at once, over real HTTP.

Eight agents hand content in (with real files) at the same moment; four
publishers race each other for the same queue; the person archives things a
publisher is grabbing, and withdraws requests an agent is answering. After
every burst an invariant checker re-derives every item's stage from its
platforms and looks for anything impossible: a post taken twice, a lost file,
an archived item still being posted, an open request on an item in Review.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from content_samples import png

from jarvis.content_manager import files
from jarvis.db import get_db

PENDING = {"scheduled", "queued", "publishing"}


def _client(base: str) -> httpx.Client:
    return httpx.Client(base_url=base, timeout=60)


def _submit(base: str, agent: str, n: int) -> dict:
    item = {"name": f"{agent} image {n}", "contentType": "image", "niche": f"Niche {n % 10}", "producer": agent,
            "fields": {"caption": f"Caption {n}", "hashtags": ["#busy"]},
            "media": [{"file": "img.png", "role": "primary"}],
            "platforms": [{"platform": "instagram"}, {"platform": "facebook"}]}
    with _client(base) as h:
        r = h.post("/api/content-items", data={"item": json.dumps(item)},
                   files=[("img.png", ("img.png", png(48, 48, (n % 255, 80, 160)), "image/png"))])
    assert r.status_code == 200, r.text
    return r.json()["item"]


def check_invariants() -> None:
    """Nothing impossible anywhere, worked out from the raw rows."""
    db = get_db()
    items = {r["id"]: r for r in db.execute("SELECT * FROM cm_items")}
    by_item: dict[str, list[str]] = {i: [] for i in items}
    for r in db.execute("SELECT item_id, status FROM cm_placements"):
        by_item[r["item_id"]].append(r["status"])
    for item_id, row in items.items():
        statuses = by_item[item_id]
        if row["stage"] in ("approved", "scheduling", "published"):
            want = "scheduling" if set(statuses) & PENDING else "published" if "published" in statuses \
                else "approved"
            assert row["stage"] == want, (item_id, row["stage"], statuses)
        if row["stage"] == "archived" or row["deleted_at"]:
            assert not set(statuses) & PENDING, ("archived/binned but still going out", item_id, statuses)
        open_requests = db.execute("SELECT COUNT(*) FROM cm_change_requests WHERE item_id = ? AND status IN "
                                   "('open','in_progress')", (item_id,)).fetchone()[0]
        assert open_requests <= 1, item_id
        if open_requests:
            assert row["stage"] == "changes_requested" and not row["deleted_at"], (item_id, row["stage"])
    # Every file on record exists on disk, and none is left belonging to nothing.
    for r in db.execute("SELECT * FROM cm_files"):
        assert files.get(r["id"]) is not None, r["id"]
    assert db.execute("SELECT COUNT(*) FROM cm_files WHERE item_id IS NULL").fetchone()[0] == 0


@pytest.fixture
def base(live_server):
    yield live_server


def test_many_hands_at_once(base):
    # --- 8 agents x 25 items, all at the same moment --------------------------------------------
    with ThreadPoolExecutor(max_workers=8) as pool:
        submitted = list(pool.map(lambda args: _submit(base, *args),
                                  [(f"Agent{a}", a * 100 + n) for a in range(8) for n in range(25)]))
    assert len({s["id"] for s in submitted}) == 200
    assert get_db().execute("SELECT COUNT(*) FROM cm_files").fetchone()[0] == 200
    check_invariants()

    # --- the person approves them all and sends 40 posts to "post now", concurrently ------------
    h = _client(base)
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda s: h.post(f"/api/content-items/{s['id']}/approve").raise_for_status(), submitted))
    queued = [s["placements"][0]["id"] for s in submitted[:40]]
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda pid: h.post(f"/api/content-placements/{pid}/post-now").raise_for_status(), queued))
    check_invariants()

    # --- four publishers race for the same queue --------------------------------------------------
    won: list[tuple[str, str]] = []
    errors: list[str] = []
    lock = threading.Lock()

    def publisher(name: str) -> None:
        with _client(base) as c:
            for _ in range(20):
                queue = c.get("/api/content-publish-queue").json()["queue"]
                if not queue:
                    return
                for job in queue:
                    r = c.post(f"/api/content-placements/{job['placementId']}/claim", json={"by": name})
                    if r.status_code >= 500:
                        errors.append(r.text)
                    if r.status_code != 200:
                        continue
                    with lock:
                        won.append((job["placementId"], name))
                    done = c.post(f"/api/content-placements/{job['placementId']}/result",
                                  json={"ok": True, "url": f"https://instagram.example/{job['placementId']}",
                                        "by": name})
                    if done.status_code != 200:
                        errors.append(done.text)

    threads = [threading.Thread(target=publisher, args=(f"PostBot{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not errors, errors[:3]
    claimed = [pid for pid, _ in won]
    assert sorted(claimed) == sorted(queued), "every queued post taken exactly once — none twice, none missed"
    published = get_db().execute("SELECT COUNT(*) FROM cm_placements WHERE status = 'published'").fetchone()[0]
    assert published == 40
    check_invariants()

    # --- archive racing a publisher's claim: exactly one of them wins ------------------------------
    racers = submitted[40:60]
    for s in racers:
        h.post(f"/api/content-placements/{s['placements'][1]['id']}/post-now").raise_for_status()
    results: dict[str, tuple[int, int]] = {}

    def race(s: dict) -> None:
        pid = s["placements"][1]["id"]
        barrier = threading.Barrier(2)
        out = {}

        def claim():
            barrier.wait()
            with _client(base) as c:
                out["claim"] = c.post(f"/api/content-placements/{pid}/claim", json={"by": "Racer"}).status_code

        def archive():
            barrier.wait()
            with _client(base) as c:
                out["archive"] = c.post(f"/api/content-items/{s['id']}/archive").status_code

        pair = [threading.Thread(target=claim), threading.Thread(target=archive)]
        for t in pair:
            t.start()
        for t in pair:
            t.join()
        results[s["id"]] = (out["claim"], out["archive"])

    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(race, racers))
    for item_id, (claim, archive) in results.items():
        assert (claim, archive) in ((200, 400), (400, 200)), (item_id, claim, archive)
    check_invariants()

    # --- an agent answering a request while the person withdraws it -------------------------------
    answering = submitted[60:80]
    requests = {}
    for s in answering:
        r = h.post(f"/api/content-items/{s['id']}/request-changes", json={"what": "Warmer colours"})
        r.raise_for_status()
        requests[s["id"]] = r.json()["item"]["openRequest"]["id"]
    outcomes = {}

    def answer_or_withdraw(s: dict) -> None:
        barrier = threading.Barrier(2)
        out = {}

        def revise():
            barrier.wait()
            with _client(base) as c:
                out["revise"] = c.post(f"/api/content-items/{s['id']}/revisions",
                                       json={"fields": {"caption": "Warmer now"}, "by": "Agent"}).status_code

        def withdraw():
            barrier.wait()
            with _client(base) as c:
                out["withdraw"] = c.post(f"/api/content-change-requests/{requests[s['id']]}/cancel").status_code

        pair = [threading.Thread(target=revise), threading.Thread(target=withdraw)]
        for t in pair:
            t.start()
        for t in pair:
            t.join()
        outcomes[s["id"]] = (out["revise"], out["withdraw"])

    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(answer_or_withdraw, answering))
    for s in answering:
        item = h.get(f"/api/content-items/{s['id']}").json()["item"]
        revise, withdraw = outcomes[s["id"]]
        assert (revise, withdraw) in ((200, 400), (400, 200)), (s["id"], revise, withdraw)
        assert item["stage"] == "review" and item["openRequest"] is None
        assert item["revision"] == (2 if revise == 200 else 1)
    check_invariants()

    # --- the screen's counts still add up with all of that done ---------------------------------------
    summary = h.get("/api/content-items/summary").json()
    total = sum(summary["counts"][s] for s in ("review", "changes_requested", "archived", "bin"))
    assert total + get_db().execute(
        "SELECT COUNT(*) FROM cm_items WHERE deleted_at IS NULL AND stage IN ('approved','scheduling','published')"
    ).fetchone()[0] == 200
