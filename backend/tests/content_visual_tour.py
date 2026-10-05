"""Screenshot tour of Content Management under a realistic load — for a person to LOOK at.

Not a test: assertions cannot tell whether a screen reads well. This seeds a
scratch Jarvis (never the real data dir, never port 3000) with `content_seed`,
opens the real front end in Chromium, walks every stage, opens every dialog,
and saves a PNG of each, at three widths. It also times how long the busiest
list takes to appear in the browser.

    python tests/content_visual_tour.py <out-dir> [item-count]

Each step is tolerant: a control the current screen doesn't have is noted in
`tour.json` and skipped, so the same tour runs before and after a change.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE)]


def _record_webms(chrome: Path) -> dict[str, bytes]:
    """Real, playable WebMs (landscape and vertical), recorded in Chromium itself."""
    import base64

    from playwright.sync_api import sync_playwright

    script = """
    async ([w, h, label]) => {
      const c = document.createElement('canvas'); c.width = w; c.height = h;
      const g = c.getContext('2d');
      const rec = new MediaRecorder(c.captureStream(24), { mimeType: 'video/webm' });
      const parts = []; rec.ondataavailable = (e) => parts.push(e.data);
      let f = 0;
      const t = setInterval(() => {
        const grd = g.createLinearGradient(0, 0, w, h);
        grd.addColorStop(0, `hsl(${(f * 6) % 360},55%,35%)`); grd.addColorStop(1, `hsl(${(f * 6 + 120) % 360},55%,25%)`);
        g.fillStyle = grd; g.fillRect(0, 0, w, h);
        g.fillStyle = '#fff'; g.font = `${Math.round(h / 9)}px sans-serif`; g.fillText(label, w * 0.08, h * 0.55); f++;
      }, 42);
      rec.start(); await new Promise((r) => setTimeout(r, 1200)); rec.stop(); clearInterval(t);
      await new Promise((r) => (rec.onstop = r));
      const bytes = new Uint8Array(await new Blob(parts, { type: 'video/webm' }).arrayBuffer());
      let s = ''; for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
      return btoa(s);
    }"""
    with sync_playwright() as play:
        browser = play.chromium.launch(executable_path=str(chrome), args=["--no-sandbox"])
        page = browser.new_page()
        page.set_content("<html><body></body></html>")
        wide = base64.b64decode(page.evaluate(script, [320, 180, "Rain on a Tin Roof"]))
        tall = base64.b64decode(page.evaluate(script, [180, 320, "Shorts"]))
        browser.close()
    return {"webm": wide, "webm_vertical": tall}


def main() -> None:
    out = Path(sys.argv[1]).resolve()
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 1500
    out.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="cm-tour-"))
    (scratch / "data").mkdir()
    os.environ["JARVIS_DATA_DIR"] = str(scratch / "data")
    os.environ["JARVIS_ENV_PATH"] = str(scratch / ".env")

    from jarvis.store import data_dir
    assert str(data_dir()).startswith(str(scratch)), "refusing to run against the real data directory"

    from test_shell_e2e import CHROME, settle
    import content_seed

    media = _record_webms(CHROME)
    content_seed.seed(count, media=media)

    import uvicorn
    from playwright.sync_api import sync_playwright

    from jarvis.main import create_app

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    report: dict = {"count": count, "shots": [], "skipped": [], "timings": {}}

    def shot(page, name: str) -> None:
        page.wait_for_timeout(350)
        path = out / f"{len(report['shots']) + 1:02d}-{name}.png"
        page.screenshot(path=str(path), full_page=False)
        report["shots"].append(path.name)

    def step(page, name: str, action) -> None:
        try:
            action()
            shot(page, name)
        except Exception as err:  # noqa: BLE001 — a missing control is a finding, not a crash
            report["skipped"].append({"step": name, "why": str(err).splitlines()[0][:200]})

    def close_all(page) -> None:
        for _ in range(4):
            if page.locator("[data-testid=modal-close]").count() == 0:
                break
            page.locator("[data-testid=modal-close]").last.click()
            page.wait_for_timeout(150)

    def card_with(page, predicate_js: str):
        """Open the first card in the current list the item JSON matches."""
        return page.evaluate(predicate_js)

    with sync_playwright() as play:
        browser = play.chromium.launch(executable_path=str(CHROME), args=["--no-sandbox"])
        for width, height, tag in ((1440, 900, "desktop"), (1280, 800, "laptop"), (1920, 1080, "wide"),
                                   (820, 1100, "narrow")):
            page = browser.new_context(viewport={"width": width, "height": height}).new_page()
            started = time.monotonic()
            page.goto(f"{base}/#/content/all", wait_until="load")
            page.locator("[data-testid=content-card]").first.wait_for(timeout=60000)
            report["timings"][f"{tag}: first cards visible"] = round((time.monotonic() - started) * 1000)
            settle(page)
            shot(page, f"{tag}-review")
            if tag != "desktop":
                if tag == "narrow":
                    step(page, f"{tag}-new-content", lambda: (page.click("[data-testid=add-content]"),
                                                                    page.click("[data-testid=add-video]")))
                    close_all(page)
                    step(page, f"{tag}-workspace", lambda: (
                        page.click("[data-testid=stage-scheduling]"),
                        page.locator("[data-testid=content-card]").first.click(),
                        page.locator("[data-testid=workspace]").wait_for(timeout=15000)))
                    close_all(page)
                for stage in ("scheduling", "published"):
                    step(page, f"{tag}-{stage}", lambda s=stage: (
                        page.click(f"[data-testid=stage-{s}]"),
                        page.locator("[data-testid=content-card]").first.wait_for(timeout=30000)))
                page.context.close()
                continue

            for stage in ("changes_requested", "approved", "scheduling", "published", "archived", "bin"):
                def go(s=stage):
                    t = time.monotonic()
                    page.click(f"[data-testid=stage-{s}]")
                    page.locator("[data-testid=content-card]").first.wait_for(timeout=60000)
                    settle(page, 3000)
                    report["timings"][f"stage {s}: cards visible"] = round((time.monotonic() - t) * 1000)
                step(page, f"{tag}-stage-{stage}", go)
            step(page, f"{tag}-scheduling-scrolled", lambda: (
                page.click("[data-testid=stage-scheduling]"), settle(page, 3000),
                page.mouse.wheel(0, 6000)))
            step(page, f"{tag}-calendar", lambda: (
                page.click("[data-testid=stage-scheduling]"), page.click("[data-testid=mode-calendar]"),
                settle(page, 3000)))
            step(page, f"{tag}-search-title", lambda: (
                page.click("[data-testid=mode-list]") if page.locator("[data-testid=mode-list]").count() else None,
                page.click("[data-testid=stage-published]"), page.fill("[data-testid=content-search]", "title"),
                page.wait_for_timeout(900)))
            page.fill("[data-testid=content-search]", "")
            step(page, f"{tag}-filters-niche", lambda: (
                page.select_option("[data-testid=filter-niche]", "Nature Sounds"), page.wait_for_timeout(900)))
            page.select_option("[data-testid=filter-niche]", "")

            # Workspaces: the busiest item (most platforms), and one of each kind.
            api = {"approved": page.request.get(f"{base}/api/content-items?stage=scheduling").json()["items"]}
            busiest = max(api["approved"], key=lambda i: len(i["placements"]))
            kinds = {}
            for s in ("review", "approved", "published", "scheduling"):
                for i in page.request.get(f"{base}/api/content-items?stage={s}").json()["items"]:
                    kinds.setdefault(i["contentType"], i)
                    if len(i["name"]) > 60:
                        kinds.setdefault("long-name", i)
            targets = [("busiest", busiest)] + sorted(kinds.items())
            for label, it in targets:
                def open_ws(it=it):
                    page.goto(f"{base}/#/content/all", wait_until="load")
                    page.click(f"[data-testid=stage-{it['stage']}]")
                    page.fill("[data-testid=content-search]", it["name"])
                    page.wait_for_timeout(900)
                    page.locator(f"[data-testid=content-card][data-item-id='{it['id']}']").click()
                    page.locator("[data-testid=workspace]").wait_for(timeout=15000)
                    settle(page, 2000)
                step(page, f"{tag}-workspace-{label}", open_ws)
                step(page, f"{tag}-workspace-{label}-scrolled", lambda: (
                    page.locator("[data-testid=workspace]").hover(), page.mouse.wheel(0, 1500)))
                if label == "busiest":
                    step(page, f"{tag}-platform-version", lambda: (
                        page.locator("[data-testid=pl-version]").first.click()))
                close_all(page)

            # Dialogs, opened for real on items that allow them. A fresh load
            # each time, so nothing left open from the last one is in the way.
            def fresh() -> None:
                page.goto(f"{base}/?t={time.monotonic()}#/content/all", wait_until="load")
                page.locator("[data-testid=content-screen]").wait_for(timeout=30000)
                settle(page, 2000)

            def open_first(stage: str, has: str | None = None) -> None:
                fresh()
                page.click(f"[data-testid=stage-{stage}]")
                cards = page.locator("[data-testid=content-card]")
                cards.first.wait_for(timeout=30000)
                if has:
                    for n in range(min(cards.count(), 50)):
                        cards.nth(n).click()
                        page.locator("[data-testid=workspace]").wait_for(timeout=15000)
                        settle(page, 800)
                        if page.locator(has).count():
                            return
                        close_all(page)
                    raise RuntimeError(f"no {stage} item has {has}")
                cards.first.click()
                page.locator("[data-testid=workspace]").wait_for(timeout=15000)
                settle(page, 1500)

            dialogs = [
                ("review", "request-changes", "[data-testid=ws-request-changes]", None),
                ("approved", "schedule", "[data-testid=ws-schedule]", None),
                ("approved", "add-platform", "[data-testid=ws-add-platform]", None),
                ("approved", "mark-posted", "[data-testid=pl-mark-posted]", None),
                ("approved", "archive-confirm", "[data-testid=ws-archive]", "[data-testid=pl-cancel]"),
                ("scheduling", "delete-confirm", "[data-testid=ws-delete]", None),
                ("bin", "purge-confirm", "[data-testid=ws-purge]", None),
                ("published", "add-numbers", "[data-testid=pl-add-numbers]", None),
                ("changes_requested", "hand-in-revision", "[data-testid=ws-hand-in]", None),
                ("review", "change-files", "[data-testid=ws-change-files]", None),
                ("approved", "platform-files", "[data-testid=pl-files]", None),
            ]
            for stage, name, selector, has in dialogs:
                step(page, f"{tag}-dialog-{name}", lambda st=stage, sel=selector, h=has: (
                    open_first(st, h or sel), page.locator(sel).first.click(), page.wait_for_timeout(400)))
                close_all(page)
            step(page, f"{tag}-published-numbers", lambda: (
                open_first("published", "[data-testid=placement-numbers] dl"),
                page.locator("[data-testid=placement-numbers] dl").first.scroll_into_view_if_needed()))
            close_all(page)

            # The new screen-level actions and views.
            step(page, f"{tag}-new-content-empty", lambda: (fresh(), page.click("[data-testid=add-content]"),
                                                                      page.click("[data-testid=add-video]")))
            step(page, f"{tag}-new-content-filled", lambda: (
                page.fill("[data-testid=new-name]", "Nature Sounds Video #001 — Rain on a Tin Roof, 10 hours"),
                page.fill("[data-testid=new-niche]", "Nature Sounds"),
                page.fill("[data-testid=new-field-title]", "Rain on a Tin Roof — 10 hours of sleep sounds"),
                page.fill("[data-testid=new-field-caption]", "Fall asleep in minutes. Headphones on."),
                page.set_input_files("[data-testid=new-files-pick-primary]", files=[
                    {"name": "rain-on-a-tin-roof-final-cut.webm", "mimeType": "video/webm", "buffer": media["webm"]}]),
                page.set_input_files("[data-testid=new-files-pick-thumbnail]", files=[
                    {"name": "thumb.png", "mimeType": "image/png", "buffer": content_seed.png(64, 36, (40, 90, 160))}]),
                [page.click(f"[data-testid=new-platform-{p}]") for p in ("youtube", "tiktok", "instagram", "facebook")],
                page.wait_for_timeout(500)))
            step(page, f"{tag}-new-content-carousel", lambda: (
                page.select_option("[data-testid=new-type]", "carousel"),
                page.set_input_files("[data-testid=new-files-pick-slide]", files=[
                    {"name": f"slide-{i}.png", "mimeType": "image/png",
                     "buffer": content_seed.png(36, 64, (30 * i, 100, 150))} for i in range(1, 8)]),
                page.wait_for_timeout(500)))
            close_all(page)
            step(page, f"{tag}-analytics", lambda: (fresh(), page.click("[data-testid=stage-analytics]"),
                                                    page.locator("[data-testid=analytics-table]").wait_for(timeout=30000)))
            step(page, f"{tag}-analytics-scrolled", lambda: page.mouse.wheel(0, 900))
            step(page, f"{tag}-show-more", lambda: (
                fresh(), page.click("[data-testid=stage-scheduling]"),
                page.locator("[data-testid=show-more]").scroll_into_view_if_needed()))
            step(page, f"{tag}-calendar-more", lambda: (
                fresh(), page.click("[data-testid=stage-scheduling]"), page.click("[data-testid=mode-calendar]"),
                settle(page, 2000), page.locator("[data-testid=cal-more]").first.click()))
            close_all(page)
            step(page, f"{tag}-calendar-day-picker", lambda: (
                fresh(), page.click("[data-testid=stage-scheduling]"), page.click("[data-testid=mode-calendar]"),
                settle(page, 2000), page.locator("[data-testid=cal-day]").last.click(position={"x": 60, "y": 8})))
            close_all(page)
            step(page, f"{tag}-empty-state", lambda: (
                fresh(), page.fill("[data-testid=content-search]", "zzzz nothing matches this"),
                page.wait_for_timeout(1200)))
            page.context.close()
        browser.close()
    server.should_exit = True
    (out / "tour.json").write_text(json.dumps(report, indent=1))
    print(json.dumps({"shots": len(report["shots"]), "skipped": report["skipped"],
                      "timings": report["timings"]}, indent=1))


if __name__ == "__main__":
    main()
