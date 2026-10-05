"""Niche folders, used the way a person uses them — under a realistic load.

Not a test: a walkthrough that clicks and types in the real front end, in
Chromium, against a scratch Jarvis in its OWN process (never the real data dir,
never port 3000), seeded through the real lifecycle with ten niches of every
kind (`content_seed.seed_niches`). After every phase an independent checker
recounts straight from the database (read-only) and compares it with what the
API and the screen show: folder totals and types, stage counts, every page of
every folder (no duplicates, nothing missing, nothing misfiled). It restarts
the server for real, times the screens, and takes screenshots at four sizes for
a person to look at.

    python tests/content_niche_tour.py <out-dir> [scale]      # scale 1 ≈ 530 items, 3 ≈ 1,550

Writes `<out-dir>/tour.json` (steps, problems, timings) and the PNGs. A step
that fails is recorded with the reason and a screenshot, and the tour goes on.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import traceback
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import httpx

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE)]

SERVE = """
import os, sys
sys.path[:0] = [sys.argv[2], sys.argv[3]]
from jarvis.store import data_dir
assert str(data_dir()).startswith(sys.argv[1]), "refusing the real data dir"
import uvicorn
from jarvis.main import create_app
uvicorn.run(create_app(), host="127.0.0.1", port=int(os.environ["PORT"]), log_level="warning")
"""

SIZES = [(1440, 900, "desktop"), (1280, 800, "laptop"), (1024, 768, "small"), (768, 1024, "tablet")]


# --- the server, in its own process -----------------------------------------------------------

def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def pids_on(port: int) -> list[int]:
    """Processes started with PORT=<port> — `/proc`, since there is no netstat here."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            env = (entry / "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        if f"PORT={port}".encode() in env:
            found.append(int(entry.name))
    return found


def start(root: Path, port: int) -> subprocess.Popen:
    env = {**os.environ, "JARVIS_DATA_DIR": str(root / "data"), "JARVIS_ENV_PATH": str(root / ".env"),
           "PORT": str(port)}
    log = open(root / "server.log", "a")
    proc = subprocess.Popen([sys.executable, "-c", SERVE, str(root), str(HERE.parent), str(HERE)],
                            env=env, stdout=log, stderr=log)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/api/content-niches", timeout=2).status_code == 200:
                return proc
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    raise RuntimeError("the scratch server did not come up")


def stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


# --- the independent checker ---------------------------------------------------------------------

def recount(db_path: Path) -> dict:
    """What the database itself says, read-only, with SQL written here — not the app's."""
    ro = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    ro.row_factory = sqlite3.Row
    folders: dict[str, dict] = {}
    names = {r["name"].lower(): r["name"] for r in ro.execute("SELECT name FROM cm_niches")}
    for key, name in names.items():
        folders[key] = {"name": name, "active": 0, "types": Counter(), "review": 0, "changes_requested": 0,
                        "archived": 0, "bin": 0, "ids": set()}
    none = {"name": "", "active": 0, "types": Counter(), "review": 0, "changes_requested": 0, "archived": 0,
            "bin": 0, "ids": set()}
    orphans = []
    for r in ro.execute("SELECT id, niche, content_type, stage, deleted_at FROM cm_items"):
        if r["niche"]:
            f = folders.get(r["niche"].lower())
            if f is None:
                orphans.append(r["id"])
                continue
            if r["niche"] != f["name"]:
                orphans.append(f"{r['id']} spelled {r['niche']!r}")
        else:
            f = none
        if r["deleted_at"]:
            f["bin"] += 1
        elif r["stage"] == "archived":
            f["archived"] += 1
        else:
            f["active"] += 1
            f["types"][r["content_type"]] += 1
            f["ids"].add(r["id"])
            if r["stage"] in ("review", "changes_requested"):
                f[r["stage"]] += 1
    ro.close()
    return {"folders": folders, "none": none, "orphans": orphans}


def check(base: str, db_path: Path, label: str, report: dict) -> list[str]:
    truth = recount(db_path)
    problems: list[str] = []
    http = httpx.Client(base_url=base, timeout=60)
    overview = http.get("/api/content-niches").json()
    if truth["orphans"]:
        problems.append(f"items whose niche is not a folder: {truth['orphans'][:5]}")
    api_names = [n["name"] for n in overview["niches"]]
    want_names = sorted((f["name"] for f in truth["folders"].values()), key=str.lower)
    if api_names != want_names:
        problems.append(f"folders differ: api {api_names} vs db {want_names}")
    everything = {"active": 0, "types": Counter(), "ids": set()}

    def folder_check(tag: str, api: dict, want: dict, params: dict) -> None:
        if api["total"] != want["active"]:
            problems.append(f"{tag}: card says {api['total']} in play, database has {want['active']}")
        if Counter(api["byType"]) != want["types"]:
            problems.append(f"{tag}: types {api['byType']} vs {dict(want['types'])}")
        if (api["archived"], api["binned"]) != (want["archived"], want["bin"]):
            problems.append(f"{tag}: archived/bin {api['archived']}/{api['binned']} vs {want['archived']}/{want['bin']}")
        summary = http.get("/api/content-items/summary", params=params).json()["counts"]
        for stage in ("active", "review", "changes_requested", "archived"):
            if summary[stage] != want[stage]:
                problems.append(f"{tag}: {stage} tab says {summary[stage]}, database has {want[stage]}")
        if summary["bin"] != want["bin"]:
            problems.append(f"{tag}: bin tab says {summary['bin']}, database has {want['bin']}")
        # Every page of the folder's "All": nothing twice, nothing missing, nothing misfiled.
        seen: list[str] = []
        offset, total = 0, None
        while True:
            page = http.get("/api/content-items", params={"stage": "active", "limit": 50, "offset": offset,
                                                         **params}).json()
            total = page["total"]
            for item in page["items"]:
                seen.append(item["id"])
                expected = want["name"]
                if tag != "All content" and item["niche"] != expected:
                    problems.append(f"{tag}: listed {item['name']!r} whose niche is {item['niche']!r}")
            offset += 50
            if not page["items"] or offset >= total:
                break
        if len(seen) != len(set(seen)):
            problems.append(f"{tag}: {len(seen) - len(set(seen))} items listed twice across pages")
        if set(seen) != want["ids"]:
            problems.append(f"{tag}: pages hold {len(set(seen))}, database {len(want['ids'])} "
                            f"(missing {len(want['ids'] - set(seen))}, extra {len(set(seen) - want['ids'])})")
        if total != want["active"]:
            problems.append(f"{tag}: list total {total} vs {want['active']}")

    for api in overview["niches"]:
        want = truth["folders"].get(api["name"].lower())
        if want is None:
            continue
        folder_check(api["name"], api, want, {"niche": api["name"]})
        everything["active"] += want["active"]
        everything["types"] += want["types"]
        everything["ids"] |= want["ids"]
    folder_check("No niche", overview["none"], truth["none"], {"noNiche": "1"})
    everything["active"] += truth["none"]["active"]
    everything["types"] += truth["none"]["types"]
    everything["ids"] |= truth["none"]["ids"]
    all_want = {"name": "", "active": everything["active"], "types": everything["types"], "ids": everything["ids"],
                "review": sum(f["review"] for f in [*truth["folders"].values(), truth["none"]]),
                "changes_requested": sum(f["changes_requested"] for f in [*truth["folders"].values(), truth["none"]]),
                "archived": sum(f["archived"] for f in [*truth["folders"].values(), truth["none"]]),
                "bin": sum(f["bin"] for f in [*truth["folders"].values(), truth["none"]])}
    folder_check("All content", overview["all"], all_want, {})
    total_folders = sum(n["total"] for n in overview["niches"]) + overview["none"]["total"]
    if total_folders != overview["all"]["total"]:
        problems.append(f"folders add up to {total_folders}, All content says {overview['all']['total']}")
    report["checks"].append({"after": label, "items": len(everything["ids"]), "folders": len(api_names),
                             "problems": problems})
    print(f"  check after {label}: {len(everything['ids'])} in play, {len(api_names)} folders, "
          f"{len(problems)} problem(s)", flush=True)
    for p in problems[:10]:
        print("    !", p, flush=True)
    return problems


def time_api(base: str, report: dict, label: str) -> None:
    http = httpx.Client(base_url=base, timeout=60)
    biggest = max(http.get("/api/content-niches").json()["niches"], key=lambda n: n["total"])["name"]
    calls = {
        "folders (first screen)": ("/api/content-niches", {}),
        "a folder's counts": ("/api/content-items/summary", {"niche": biggest}),
        "a folder's first page": ("/api/content-items", {"stage": "active", "niche": biggest, "limit": 50}),
        "all content, first page": ("/api/content-items", {"stage": "active", "limit": 50}),
        "search everything": ("/api/content-items", {"stage": "active", "q": "habit", "limit": 50}),
        "search in a folder": ("/api/content-items", {"stage": "active", "niche": biggest, "q": "video",
                                                      "limit": 50}),
        "No niche counts": ("/api/content-items/summary", {"noNiche": "1"}),
        "a folder's analytics": ("/api/content-analytics", {"niche": biggest}),
    }
    out = {}
    for name, (path, params) in calls.items():
        took = []
        for _ in range(7):
            started = time.perf_counter()
            assert http.get(path, params=params).status_code == 200
            took.append((time.perf_counter() - started) * 1000)
        out[name] = round(statistics.median(took), 1)
    report["timings"][label] = out
    print(f"  timings ({label}):", out, flush=True)


# --- the walkthrough -----------------------------------------------------------------------------

def main() -> None:
    out = Path(sys.argv[1]).resolve()
    scale = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    out.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="cm-niche-tour-"))
    (root / "data").mkdir()
    os.environ["JARVIS_DATA_DIR"] = str(root / "data")
    os.environ["JARVIS_ENV_PATH"] = str(root / ".env")
    from jarvis.store import data_dir
    assert str(data_dir()).startswith(str(root)), "refusing to run against the real data directory"

    from content_samples import png
    from test_shell_e2e import CHROME
    import content_seed
    from content_visual_tour import _record_webms
    from jarvis.db import get_db

    report: dict = {"scale": scale, "root": str(root), "steps": [], "checks": [], "timings": {}, "shots": []}
    media = _record_webms(CHROME)
    get_db()
    started = time.monotonic()
    seeded = content_seed.seed_niches(scale=scale, media=media)
    report["seeded"] = {"items": len(seeded.items), "seconds": round(time.monotonic() - started, 1),
                        "plan": dict(Counter(seeded.plan.values()))}
    print("seeded", report["seeded"], flush=True)
    db_path = root / "data" / "jarvis.db"

    port = free_port()
    proc = start(root, port)
    base = f"http://127.0.0.1:{port}"
    report["server"] = {"port": port, "pid": proc.pid, "pids_on_port": pids_on(port)}
    agent = httpx.Client(base_url=base, timeout=60)
    check(base, db_path, "seeding", report)
    time_api(base, report, f"{len(seeded.items)} items")

    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as play:
        browser = play.chromium.launch(executable_path=str(CHROME), args=["--no-sandbox"])
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        console: list[str] = []
        page.on("pageerror", lambda e: console.append(f"pageerror: {e}"))
        page.on("console", lambda m: console.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)

        def shot(name: str, full: bool = False) -> None:
            page.wait_for_timeout(300)
            path = out / f"{len(report['shots']) + 1:03d}-{name}.png"
            page.screenshot(path=str(path), full_page=full)
            report["shots"].append(path.name)

        def step(name: str, action, *, capture: bool = True) -> bool:
            print("step:", name, flush=True)
            try:
                action()
                if capture:
                    shot(name)
                report["steps"].append({"step": name, "ok": True})
                return True
            except Exception as err:  # noqa: BLE001 — a failure is a finding; the tour goes on
                report["steps"].append({"step": name, "ok": False, "why": str(err).splitlines()[0][:300],
                                        "trace": traceback.format_exc()[-1500:]})
                print("  FAILED:", str(err).splitlines()[0][:300], flush=True)
                try:
                    shot(f"FAILED-{name}")
                    close_all()     # nothing left open to block the next step
                except Exception:  # noqa: BLE001
                    pass
                return False

        loads = Counter()

        def go(hash_: str) -> None:
            # A fresh page each time: going to the address already open keeps the tab it was on.
            loads["n"] += 1
            page.goto(f"{base}/?v={loads['n']}{hash_}", wait_until="load")
            page.locator("[data-testid=content-screen]").wait_for(timeout=30000)
            page.wait_for_timeout(600)

        def close_all() -> None:
            for _ in range(5):
                if page.locator("[data-testid=modal-close]").count() == 0:
                    break
                page.locator("[data-testid=modal-close]").last.click()
                page.wait_for_timeout(200)

        def card(name: str):
            return page.locator(f"[data-testid=content-card][data-name='{name}']")

        def folder(name: str):
            return page.locator(f"[data-testid=niche-card][data-niche='{name}']")

        def row(platform: str):
            return page.locator(f"[data-testid=ws-placement][data-platform={platform}]")

        def item_named(name: str) -> dict:
            found = agent.get("/api/content-items", params={"q": name}).json()["items"]
            found += agent.get("/api/content-items", params={"stage": "bin", "q": name}).json()["items"]
            match = [i for i in found if i["name"] == name]
            assert len(match) == 1, f"{name!r}: {len(match)} matches"
            return match[0]

        def overview() -> dict:
            return agent.get("/api/content-niches").json()

        def folder_total(name: str) -> int:
            return next(n["total"] for n in overview()["niches"] if n["name"] == name)

        def ui_grid_matches() -> None:
            ov = overview()
            for n in ov["niches"]:
                expect(folder(n["name"])).to_have_attribute("data-total", str(n["total"]))
            expect(page.locator("[data-testid=folder-all]")).to_have_attribute("data-total", str(ov["all"]["total"]))

        # 1. The first screen.
        def first_screen():
            t0 = time.monotonic()
            go("#/content")
            page.locator("[data-testid=niche-card]").first.wait_for()
            report["timings"]["browser: folders visible (ms)"] = round((time.monotonic() - t0) * 1000)
            ui_grid_matches()
            expect(folder("Astronomy")).to_contain_text("Empty")
            expect(page.locator("[data-testid=folder-none]")).to_be_visible()
        step("01-first-screen", first_screen)
        shot("01b-first-screen-full", full=True)

        # 2. A new niche, empty.
        def new_niche():
            page.click("[data-testid=new-niche]")
            page.fill("[data-testid=niche-name]", "Stoicism")
            shot("02a-new-niche-dialog")
            page.click("[data-testid=niche-save]")
            expect(page.locator("[data-testid=folder-name]")).to_have_text("Stoicism")
            expect(page.locator("[data-testid=content-screen]")).to_contain_text("Nothing in “Stoicism” yet.")
        step("02-new-empty-niche", new_niche)

        # 3. One piece, niche filled in.
        thumb = png(64, 36, (40, 90, 160))

        def add_one():
            page.click("[data-testid=add-content]")
            shot("03a-add-menu")
            page.click("[data-testid=add-video]")
            expect(page.locator("[data-testid=new-niche]")).to_have_value("Stoicism")
            page.fill("[data-testid=new-name]", "The Dichotomy of Control")
            page.fill("[data-testid=new-field-title]", "What you can and can't control")
            page.fill("[data-testid=new-field-caption]", "The oldest trick for a calmer mind")
            page.set_input_files("[data-testid=new-files-pick-primary]",
                                 files=[{"name": "control.webm", "mimeType": "video/webm", "buffer": media["webm"]}])
            page.set_input_files("[data-testid=new-files-pick-thumbnail]",
                                 files=[{"name": "thumb.png", "mimeType": "image/png", "buffer": thumb}])
            page.click("[data-testid=new-platform-tiktok]")
            page.click("[data-testid=new-platform-youtube]")
            shot("03b-new-video-filled")
            page.click("[data-testid=new-send-review]")
            expect(page.locator("[data-testid=workspace]")).to_be_visible()
            shot("03c-workspace-new-video")
            close_all()
            expect(page.locator("[data-testid=stage-active]")).to_have_attribute("data-count", "1")
            assert item_named("The Dichotomy of Control")["niche"] == "Stoicism"
        step("03-add-one-video", add_one)

        # 4. Several files at once.
        def add_several_videos():
            page.click("[data-testid=add-content]")
            page.click("[data-testid=add-several]")
            files = [{"name": f"stoic_quote-{n:02d}.webm", "mimeType": "video/webm",
                      "buffer": media["webm_vertical" if n % 2 else "webm"]} for n in range(1, 11)]
            page.set_input_files("[data-testid=several-files]", files=files)
            expect(page.locator("[data-testid=several-row]")).to_have_count(10)
            page.locator("[data-testid=several-name]").nth(0).fill("Memento Mori")
            page.locator("[data-testid=several-name]").nth(1).fill("Amor Fati")
            page.click("[data-testid=several-platform-tiktok]")
            page.click("[data-testid=several-platform-youtube]")
            shot("04a-add-several-ready")
            page.click("[data-testid=several-review]")
            expect(page.locator("[data-testid=several]")).to_have_count(0, timeout=60000)
            expect(page.locator("[data-testid=stage-active]")).to_have_attribute("data-count", "11")
            expect(page.locator("[data-testid=type-video]")).to_have_attribute("data-count", "11")
            stored = agent.get("/api/content-items", params={"stage": "active", "niche": "Stoicism"}).json()["items"]
            names = sorted(i["name"] for i in stored)
            assert len(stored) == 11 and len({i["id"] for i in stored}) == 11, names
            assert "Memento Mori" in names and "Amor Fati" in names and "stoic quote 10" in names, names
            assert all(i["stage"] == "review" for i in stored)
            assert all(sorted(p["platform"] for p in i["placements"]) == ["tiktok", "youtube"] for i in stored)
        step("04-add-several-videos", add_several_videos)

        def add_several_images_with_a_bad_file():
            page.click("[data-testid=add-content]")
            page.click("[data-testid=add-several]")
            page.select_option("[data-testid=several-type]", "image")
            files = [{"name": f"quote-card-{n}.png", "mimeType": "image/png",
                      "buffer": png(40, 40, (20 * n, 90, 140))} for n in range(1, 7)]
            files.append({"name": "notes.txt", "mimeType": "text/plain", "buffer": b"not an image"})
            page.set_input_files("[data-testid=several-files]", files=files)
            expect(page.locator("[data-testid=several-row]")).to_have_count(7)
            expect(page.locator("[data-testid=several-error]")).to_have_count(1)     # the text file, marked
            shot("04b-add-several-images-with-a-bad-file")
            page.click("[data-testid=several-ready]")
            expect(page.locator("[data-testid=several-progress]")).to_contain_text("6 added · 1 not added",
                                                                                   timeout=60000)
            shot("04c-add-several-finished-with-one-refused")
            close_all()
            expect(page.locator("[data-testid=stage-approved]")).to_have_attribute("data-count", "6")
            expect(page.locator("[data-testid=stage-active]")).to_have_attribute("data-count", "17")
        step("04-add-several-images", add_several_images_with_a_bad_file)
        check(base, db_path, "adding content", report)

        # 5. The full lifecycle of one piece, inside its niche.
        name = "The Dichotomy of Control"

        def in_stoicism(stage_tab: str, expect_stage: str | None = None):
            item = item_named(name)
            assert item["niche"] == "Stoicism", item["niche"]
            if expect_stage:
                assert (item["stage"] if not item["deletedAt"] else "bin") == expect_stage, item["stage"]
            page.click(f"[data-testid=stage-{stage_tab}]")
            expect(card(name)).to_have_count(1)

        def open_it(tab: str):
            page.click(f"[data-testid=stage-{tab}]")
            card(name).click()
            expect(page.locator("[data-testid=workspace]")).to_be_visible()

        def lifecycle_review_to_changes():
            in_stoicism("review", "review")
            open_it("review")
            page.click("[data-testid=ws-request-changes]")
            page.fill("[data-testid=changes-what]", "Open on the quote, not the intro")
            page.click("[data-testid=send-changes]")
            expect(page.locator("[data-testid=workspace]")).to_have_attribute("data-stage", "changes_requested")
            shot("05a-changes-requested")
            close_all()
            in_stoicism("changes_requested", "changes_requested")
        step("05a-review-to-changes-requested", lifecycle_review_to_changes, capture=False)

        def lifecycle_hand_in_and_approve():
            open_it("changes_requested")
            page.click("[data-testid=ws-hand-in]")
            page.fill("[data-testid=revise-field-caption]", "Start with the quote. Then the calm.")
            page.fill("[data-testid=revise-note]", "Opened on the quote")
            shot("05b-hand-in-revision")
            page.click("[data-testid=revise-save]")
            expect(page.locator("[data-testid=workspace]")).to_have_attribute("data-stage", "review")
            page.click("[data-testid=ws-approve]")
            expect(page.locator("[data-testid=workspace]")).to_have_attribute("data-stage", "approved")
            close_all()
            in_stoicism("approved", "approved")
        step("05b-revision-then-ready-to-post", lifecycle_hand_in_and_approve, capture=False)

        def lifecycle_platforms_and_files():
            open_it("approved")
            page.click("[data-testid=ws-add-platform]")
            page.select_option("[data-testid=schedule-platform]", "instagram")
            page.fill("[data-testid=schedule-destination]", "Reels")
            page.click("[data-testid=schedule-save]")
            expect(row("instagram")).to_contain_text("Reels")
            row("tiktok").locator("[data-testid=pl-files]").click()
            page.set_input_files("[data-testid=pl-files-slots-pick-primary]", files=[
                {"name": "control-vertical.webm", "mimeType": "video/webm", "buffer": media["webm_vertical"]}])
            shot("05c-tiktok-own-file")
            page.click("[data-testid=pl-files-save]")
            page.wait_for_timeout(800)
            stored = agent.get(f"/api/content-items/{item_named(name)['id']}").json()["item"]
            tiktok = next(p for p in stored["placements"] if p["platform"] == "tiktok")
            assert [m["role"] for m in tiktok["media"]] == ["primary"], tiktok["media"]
            close_all()
        step("05c-add-platform-and-a-platform-file", lifecycle_platforms_and_files, capture=False)

        day3 = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
        day4 = (datetime.now() + timedelta(days=4)).strftime("%Y-%m-%d")

        def lifecycle_schedule_from_calendar():
            page.click("[data-testid=stage-scheduling]")
            page.click("[data-testid=mode-calendar]")
            target = datetime.now() + timedelta(days=3)
            for _ in range((target.year - datetime.now().year) * 12 + target.month - datetime.now().month):
                page.click("[data-testid=cal-next]")
            shot("05d-calendar-in-niche")
            page.locator(f"[data-testid=cal-day][data-date='{day3}']").click(position={"x": 60, "y": 70})
            items = page.locator("[data-testid=pick-ready-item]")
            expect(items.first).to_be_visible()
            texts = items.all_inner_texts()
            assert all("Stoicism" in t for t in texts), texts          # only this folder's content
            shot("05e-pick-from-this-niche")
            page.locator("[data-testid=pick-ready-item]", has_text=name).click()
            page.select_option("[data-testid=schedule-target]", label="TikTok")
            page.fill("[data-testid=schedule-time]", "18:30")
            page.click("[data-testid=schedule-save]")
            expect(row("tiktok")).to_have_attribute("data-status", "scheduled")
            row("youtube").locator("[data-testid=pl-schedule]").click()
            page.fill("[data-testid=schedule-date]", day4)
            page.fill("[data-testid=schedule-time]", "09:00")
            page.click("[data-testid=schedule-save]")
            expect(page.locator("[data-testid=workspace]")).to_have_attribute("data-stage", "scheduling")
            close_all()
            page.click("[data-testid=mode-list]")
            in_stoicism("scheduling", "scheduling")
        step("05d-schedule-from-the-niche-calendar", lifecycle_schedule_from_calendar, capture=False)

        def lifecycle_publish():
            open_it("scheduling")
            row("instagram").locator("[data-testid=pl-post-now]").click()
            expect(row("instagram")).to_have_attribute("data-status", "queued")
            job = next(j for j in agent.get("/api/content-publish-queue").json()["queue"]
                       if j["name"] == name and j["platform"] == "instagram")
            assert job["niche"] == "Stoicism", job["niche"]           # the publisher sees the niche too
            agent.post(f"/api/content-placements/{job['placementId']}/claim", json={"by": "PostBot"})
            agent.post(f"/api/content-placements/{job['placementId']}/result",
                       json={"ok": True, "url": "https://instagram.example/reel/STOIC1", "by": "PostBot"})
            expect(row("instagram")).to_have_attribute("data-status", "published", timeout=10000)
            row("youtube").locator("[data-testid=pl-mark-posted]").click()
            page.fill("[data-testid=mark-posted-url]", "https://youtube.example/shorts/STOIC1")
            page.click("[data-testid=mark-posted-save]")
            expect(row("youtube")).to_have_attribute("data-status", "published")
            row("instagram").locator("[data-testid=pl-add-numbers]").click()
            page.fill("[data-testid=numbers-views]", "12500")
            page.fill("[data-testid=numbers-likes]", "830")
            page.click("[data-testid=numbers-save]")
            page.wait_for_timeout(600)
            shot("05f-published-with-numbers")
            close_all()
            in_stoicism("published")
            page.click("[data-testid=stage-analytics]")
            expect(page.locator("[data-testid=analytics-row]", has_text=name).first).to_be_visible()
            posts = agent.get("/api/content-analytics", params={"niche": "Stoicism"}).json()["posts"]
            assert posts and all(p["niche"] == "Stoicism" for p in posts)
            expect(page.locator("[data-testid=analytics-row]")).to_have_count(len(posts))
            shot("05g-analytics-in-niche")
        step("05e-post-now-publisher-and-mark-posted", lifecycle_publish, capture=False)

        def lifecycle_archive_bin_restore():
            open_it("published")
            page.click("[data-testid=ws-archive]")
            if page.locator("[data-testid=confirm-yes]").count():
                page.click("[data-testid=confirm-yes]")
            expect(page.locator("[data-testid=workspace]")).to_have_attribute("data-stage", "archived")
            close_all()
            in_stoicism("archived", "archived")
            shot("05h-archived-in-niche")
            open_it("archived")
            page.click("[data-testid=ws-unarchive]")
            expect(page.locator("[data-testid=workspace]")).not_to_have_attribute("data-stage", "archived")
            page.click("[data-testid=ws-delete]")
            if page.locator("[data-testid=confirm-yes]").count():
                page.click("[data-testid=confirm-yes]")
            expect(page.locator("[data-testid=workspace]")).to_have_count(0)
            in_stoicism("bin", "bin")
            shot("05i-recycle-bin-in-niche")
            expect(page.locator("[data-testid=empty-bin]")).to_have_count(0)   # never empties other niches
            open_it("bin")
            page.click("[data-testid=ws-restore]")
            expect(page.locator("[data-testid=workspace]")).not_to_have_attribute("data-stage", "bin")
            close_all()
            in_stoicism("published", "published")
            page.click("[data-testid=back-to-niches]")
            expect(folder("Stoicism")).to_have_attribute("data-total", str(folder_total("Stoicism")))
        step("05f-archive-unarchive-bin-restore", lifecycle_archive_bin_restore, capture=False)
        check(base, db_path, "the full lifecycle", report)

        # 6. Filtering, searching and paging in a big folder.
        def filtering():
            go("#/content/niche/Psychology")
            ov = next(n for n in overview()["niches"] if n["name"] == "Psychology")
            expect(page.locator("[data-testid=stage-active]")).to_have_attribute("data-count", str(ov["total"]))
            shot("06a-psychology-all")
            for t, n in ov["byType"].items():
                expect(page.locator(f"[data-testid=type-{t}]")).to_have_attribute("data-count", str(n))
            page.click("[data-testid=type-video]")
            expect(page.locator("[data-testid=content-list-count]")).to_contain_text(f"{ov['byType']['video']} item")
            shot("06b-psychology-videos")
            page.click("[data-testid=type-all]")
            for stage in ("review", "changes_requested", "approved", "scheduling", "published", "archived", "bin"):
                page.click(f"[data-testid=stage-{stage}]")
                count = page.locator(f"[data-testid=stage-{stage}]").get_attribute("data-count")
                if count == "0":
                    expect(page.locator("[data-testid=content-card]")).to_have_count(0)
                else:
                    expect(page.locator("[data-testid=content-list-count]")).to_contain_text(f"{count} item")
                    niches = set(page.locator("[data-testid=content-card]").evaluate_all(
                        "els => els.map(e => e.dataset.niche)"))
                    assert niches == {"Psychology"}, (stage, niches)
            page.click("[data-testid=stage-active]")
            page.fill("[data-testid=content-search]", "procrastinate")
            page.wait_for_timeout(900)
            got = page.locator("[data-testid=content-card]").evaluate_all(
                "els => els.map(e => [e.dataset.name, e.dataset.niche])")
            assert got and all(n == "Psychology" and "Procrastinate" in name_ for name_, n in got), got
            shot("06c-search-in-niche")
            page.fill("[data-testid=content-search]", "")
            page.select_option("[data-testid=filter-platform]", "tiktok")
            page.wait_for_timeout(700)
            shot("06d-platform-filter")
            page.select_option("[data-testid=filter-platform]", "")
        step("06a-filters-inside-a-niche", filtering, capture=False)

        def search_everywhere():
            go("#/content/all")
            page.fill("[data-testid=content-search]", "habit")
            page.wait_for_timeout(900)
            niches = set(page.locator("[data-testid=content-card]").evaluate_all("els => els.map(e => e.dataset.niche)"))
            assert len(niches) >= 1, niches
            shot("06e-search-all-content")
            page.fill("[data-testid=content-search]", "")
            page.wait_for_timeout(900)                              # the search waits for a pause in typing
            page.click("[data-testid=stage-published]")
            month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
            before = int(page.locator("[data-testid=stage-published]").get_attribute("data-count"))
            page.fill("[data-testid=filter-from]", month_ago)
            page.wait_for_timeout(900)
            text = page.locator("[data-testid=content-list-count]").inner_text()
            shown = int(text.split(" ")[0].replace(",", ""))
            assert 0 < shown < before, (shown, before)
            shot("06f-published-last-30-days")
        step("06b-search-and-dates-across-niches", search_everywhere, capture=False)

        def show_more_everything():
            go("#/content/all")
            total = int(page.locator("[data-testid=stage-active]").get_attribute("data-count"))
            while page.locator("[data-testid=show-more]").count():
                page.click("[data-testid=show-more]")
                page.wait_for_timeout(300)
            ids = page.locator("[data-testid=content-card]").evaluate_all("els => els.map(e => e.dataset.itemId)")
            assert len(ids) == total == len(set(ids)), (len(ids), total, len(set(ids)))
            shot("06g-show-more-to-the-end")
        step("06c-show-more-through-all-content", show_more_everything, capture=False)

        # 7. Moving one piece to another niche.
        def move_one():
            go("#/content/niche/Psychology")
            before = (folder_total("Psychology"), folder_total("Fitness"))
            first = page.locator("[data-testid=content-card]").first
            moving = first.get_attribute("data-name")
            first.click()
            page.fill("[data-testid=ws-niche]", "Fitness")
            shot("07a-moving-to-fitness")
            page.click("[data-testid=ws-save]")
            expect(page.locator("[data-testid=ws-save]")).to_have_count(0)
            close_all()
            expect(card(moving)).to_have_count(0)
            assert (folder_total("Psychology"), folder_total("Fitness")) == (before[0] - 1, before[1] + 1)
            assert item_named(moving)["niche"] == "Fitness"
        step("07-move-between-niches", move_one, capture=False)

        # 8. Renaming a busy niche.
        def rename_busy():
            ro = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            snap = sorted(ro.execute("SELECT p.id, p.status, p.scheduled_at FROM cm_placements p JOIN cm_items i "
                                     "ON i.id = p.item_id WHERE i.niche = 'Travel'").fetchall())
            count = ro.execute("SELECT COUNT(*) FROM cm_items WHERE niche = 'Travel'").fetchone()[0]
            ro.close()
            go("#/content/niche/Travel")
            page.click("[data-testid=rename-niche]")
            page.fill("[data-testid=niche-name]", "Travel & Adventure")
            shot("08a-rename-dialog")
            page.click("[data-testid=niche-save]")
            expect(page.locator("[data-testid=folder-name]")).to_have_text("Travel & Adventure")
            assert page.evaluate("location.hash") == f"#/content/niche/{quote('Travel & Adventure', safe='')}"
            ro = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            after = sorted(ro.execute("SELECT p.id, p.status, p.scheduled_at FROM cm_placements p JOIN cm_items i "
                                      "ON i.id = p.item_id WHERE i.niche = 'Travel & Adventure'").fetchall())
            moved = ro.execute("SELECT COUNT(*) FROM cm_items WHERE niche = 'Travel & Adventure'").fetchone()[0]
            left = ro.execute("SELECT COUNT(*) FROM cm_items WHERE niche = 'Travel'").fetchone()[0]
            ro.close()
            assert (moved, left) == (count, 0) and after == snap, (moved, left, len(after), len(snap))
            page.click("[data-testid=rename-niche]")
            page.fill("[data-testid=niche-name]", "fitness")
            page.click("[data-testid=niche-save]")
            expect(page.locator("[data-testid=niche-error]")).to_contain_text("never merged")
            shot("08b-rename-onto-existing-refused")
            close_all()
            page.reload()
            expect(page.locator("[data-testid=folder-name]")).to_have_text("Travel & Adventure")
        step("08-rename-a-busy-niche", rename_busy, capture=False)

        # 9. Deleting.
        def delete_rules():
            page.click("[data-testid=delete-niche]")
            expect(page.locator("[data-testid=niche-delete-body]")).to_contain_text("still holds")
            shot("09a-delete-refused")
            page.click("[data-testid=niche-delete-ok]")
            go("#/content")
            page.click("[data-testid=new-niche]")
            page.fill("[data-testid=niche-name]", "Bin Only")
            page.click("[data-testid=niche-save]")
            page.click("[data-testid=add-content]")
            page.click("[data-testid=add-text_post]")
            page.fill("[data-testid=new-name]", "A thought I deleted")
            page.fill("[data-testid=new-field-body]", "Never mind.")
            page.click("[data-testid=new-send-review]")
            expect(page.locator("[data-testid=workspace]")).to_be_visible()
            page.click("[data-testid=ws-delete]")
            if page.locator("[data-testid=confirm-yes]").count():
                page.click("[data-testid=confirm-yes]")
            expect(page.locator("[data-testid=workspace]")).to_have_count(0)
            page.click("[data-testid=delete-niche]")
            expect(page.locator("[data-testid=niche-delete-body]")).to_contain_text("in the Recycle Bin")
            shot("09b-delete-refused-bin-only")
            page.click("[data-testid=niche-delete-ok]")
            page.click("[data-testid=stage-bin]")
            card("A thought I deleted").click()
            page.click("[data-testid=ws-purge]")
            page.click("[data-testid=confirm-yes]")
            expect(page.locator("[data-testid=workspace]")).to_have_count(0)
            page.click("[data-testid=delete-niche]")
            expect(page.locator("[data-testid=niche-delete-yes]")).to_be_visible()
            shot("09c-delete-empty-niche")
            page.click("[data-testid=niche-delete-yes]")
            expect(page.locator("[data-testid=niche-grid]")).to_be_visible()
            expect(folder("Bin Only")).to_have_count(0)
        step("09-delete-only-when-empty", delete_rules, capture=False)
        check(base, db_path, "move, rename and delete", report)

        # 10. No niche.
        def no_niche():
            go("#/content/none")
            shot("10a-no-niche-folder")
            loose = agent.get("/api/content-items", params={"stage": "active", "noNiche": "1"}).json()["items"]
            archived = agent.get("/api/content-items", params={"stage": "archived", "noNiche": "1"}).json()["items"]
            binned = agent.get("/api/content-items", params={"stage": "bin", "noNiche": "1"}).json()["items"]
            # An agent files most of them; the person files the last one on the screen.
            for item in [*loose[1:], *archived]:
                assert agent.patch(f"/api/content-items/{item['id']}", json={"niche": "Psychology"}).status_code == 200
            for item in binned:
                assert agent.delete(f"/api/content-items/{item['id']}/permanent").status_code == 200
            last = loose[0]["name"]
            page.reload()
            expect(page.locator("[data-testid=stage-active]")).to_have_attribute("data-count", "1")
            card(last).click()
            page.fill("[data-testid=ws-niche]", "Mindfulness")
            page.click("[data-testid=ws-save]")
            close_all()
            expect(page.locator("[data-testid=content-screen]")).to_contain_text("Everything has a niche.")
            shot("10b-no-niche-empty")
            page.click("[data-testid=back-to-niches]")
            expect(page.locator("[data-testid=folder-none]")).to_have_count(0)
        step("10-no-niche-empties-and-goes", no_niche, capture=False)

        # 11. Arriving from outside while the screen is open.
        def from_outside():
            go("#/content/niche/Psychology")
            before = int(page.locator("[data-testid=stage-active]").get_attribute("data-count"))
            for n in range(5):
                r = agent.post("/api/content-items", json={
                    "name": f"Agent drop {n}", "contentType": "text_post", "niche": "psychology",
                    "producer": "Writer", "fields": {"body": "x"}})
                assert r.status_code == 200, r.text
            expect(page.locator("[data-testid=stage-active]")).to_have_attribute("data-count", str(before + 5),
                                                                                timeout=10000)
            r = agent.post("/api/content-items", json={"name": "First of a new niche", "contentType": "text_post",
                                                       "niche": "Philosophy", "producer": "Writer",
                                                       "fields": {"body": "x"}})
            assert r.status_code == 200
            page.click("[data-testid=back-to-niches]")
            expect(folder("Philosophy")).to_have_attribute("data-total", "1", timeout=10000)
            shot("11-new-niche-from-an-agent")
        step("11-content-arriving-while-open", from_outside, capture=False)
        check(base, db_path, "agents handing in", report)

        # 12. Refresh and a real restart.
        def restart():
            nonlocal proc
            go("#/content/niche/Psychology")
            page.click("[data-testid=stage-review]")
            page.reload()
            expect(page.locator("[data-testid=folder-name]")).to_have_text("Psychology")
            old = proc.pid
            stop(proc)
            assert old not in pids_on(port), f"old server {old} still holds PORT={port}"
            proc = start(root, port)
            report["server"]["restarted"] = {"old_pid": old, "new_pid": proc.pid, "pids_on_port": pids_on(port)}
            assert pids_on(port) == [proc.pid], pids_on(port)
            go("#/content")
            ui_grid_matches()
            shot("12-after-restart")
        step("12-refresh-and-restart", restart, capture=False)
        check(base, db_path, "restart", report)

        # --- the look, at four sizes ---------------------------------------------------------------
        for width, height, tag in SIZES:
            page.set_viewport_size({"width": width, "height": height})
            step(f"look-{tag}-grid", lambda: (go("#/content"), page.locator("[data-testid=niche-card]").first.wait_for()))
            step(f"look-{tag}-busy-folder", lambda: go("#/content/niche/Psychology"))
            step(f"look-{tag}-long-name-folder",
                 lambda: go(f"#/content/niche/{quote('Productivity, Habits & Deep Work for Busy Creators', safe='')}"))
            step(f"look-{tag}-empty-folder", lambda: go("#/content/niche/Astronomy"))
            step(f"look-{tag}-add-menu", lambda: (go("#/content/niche/Psychology"),
                                                  page.click("[data-testid=add-content]")))
            close_all()
            step(f"look-{tag}-add-several", lambda: (
                page.keyboard.press("Escape"), page.click("[data-testid=add-content]"),
                page.click("[data-testid=add-several]"),
                page.set_input_files("[data-testid=several-files]", files=[
                    {"name": f"a-really-long-file-name-for-a-clip-number-{n}.webm", "mimeType": "video/webm",
                     "buffer": media["webm"]} for n in range(1, 8)])))
            close_all()
            step(f"look-{tag}-scheduling-calendar", lambda: (
                go("#/content/niche/Fitness"), page.click("[data-testid=stage-scheduling]"),
                page.click("[data-testid=mode-calendar]")))
            step(f"look-{tag}-analytics", lambda: (go("#/content/niche/Fitness"),
                                                   page.click("[data-testid=stage-analytics]")))
            step(f"look-{tag}-archived", lambda: (go("#/content/niche/Fitness"),
                                                  page.click("[data-testid=stage-archived]")))
            step(f"look-{tag}-workspace", lambda: (go("#/content/niche/Fitness"),
                                                   page.locator("[data-testid=content-card]").first.click(),
                                                   page.locator("[data-testid=workspace]").wait_for()))
            close_all()
            step(f"look-{tag}-rename-dialog", lambda: (go("#/content/niche/Fitness"),
                                                       page.click("[data-testid=rename-niche]")))
            close_all()
            step(f"look-{tag}-delete-refused", lambda: (go("#/content/niche/Fitness"),
                                                        page.click("[data-testid=delete-niche]")))
            close_all()
            # Nothing on the page may be wider than the window.
            for where in ("#/content", "#/content/niche/Psychology"):
                go(where)
                wide = page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
                report["steps"].append({"step": f"no-sideways-scroll-{tag}-{where}", "ok": not wide})

        page.set_viewport_size({"width": 1440, "height": 900})
        t0 = time.monotonic()
        go("#/content/niche/Psychology")
        page.locator("[data-testid=content-card]").first.wait_for()
        report["timings"]["browser: open a busy folder (ms)"] = round((time.monotonic() - t0) * 1000)
        report["console"] = console[:50]
        browser.close()

    time_api(base, report, "after the tour")
    stop(proc)
    report["failed_steps"] = [s for s in report["steps"] if not s["ok"]]
    report["problems"] = sum(len(c["problems"]) for c in report["checks"])
    (out / "tour.json").write_text(json.dumps(report, indent=2))
    print(f"\n{len(report['steps'])} steps, {len(report['failed_steps'])} failed; "
          f"{report['problems']} counting problems; {len(report['shots'])} screenshots in {out}")


if __name__ == "__main__":
    main()
