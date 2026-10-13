"""Scratch probe (not committed): the whole spoken path for real, with timings.

Chromium's fake microphone plays a recorded sentence; the Speak-and-listen engine streams it
to /api/duplex; Moonshine on this computer writes it down; the engine sends it as a turn; a
stub model answers. Run with JARVIS_TEST_SPEECH_MODELS and MIC_WAV set.
"""
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, "tests")
from test_shell_e2e import CHROME, connect_and_select, serve_provider, visit  # noqa: F401,E402

from playwright.sync_api import sync_playwright  # noqa: E402


def test_probe(live_server, serve_provider):  # noqa: F811
    from jarvis import speech_models

    models = Path(os.environ["JARVIS_TEST_SPEECH_MODELS"])
    root = speech_models.root()
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(models / "moonshine-base-en", root / "moonshine-base-en")
    (root / "silero-vad").mkdir()
    shutil.copy(models / "silero_vad.onnx", root / "silero-vad" / "silero_vad.onnx")

    stub = serve_provider("openai-chat", reply="Here is your calendar.")
    connect_and_select(live_server, stub)
    with sync_playwright() as play:
        browser = play.chromium.launch(executable_path=str(CHROME), args=[
            "--no-sandbox", "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
            f"--use-file-for-fake-audio-capture={os.environ['MIC_WAV']}"])
        context = browser.new_context(viewport={"width": 1440, "height": 900}, permissions=["microphone"])
        page = context.new_page()
        visit(page, live_server)
        page.evaluate("""() => {
            window.__marks = [];
            const t0 = performance.now();
            new MutationObserver(() => {
              const s = document.querySelector('[data-testid=status]')?.textContent || '';
              const last = window.__marks[window.__marks.length - 1];
              if (!last || last[1] !== s) window.__marks.push([Math.round(performance.now() - t0), s]);
            }).observe(document.body, { subtree: true, childList: true, characterData: true });
        }""")
        page.click("[data-testid=settings]")
        page.click("[data-testid=listening-select]")
        page.click("[data-testid=engine-pipeline]")
        page.keyboard.press("Escape")
        clicked = time.perf_counter()
        page.click("[data-testid=mic]")
        page.wait_for_function("() => document.body.innerText.includes('Here is your calendar.')", timeout=120_000)
        print(f"\nclick -> reply on screen: {time.perf_counter() - clicked:.1f}s")
        said = page.evaluate("() => [...document.querySelectorAll('[data-testid=transcript] > div')].map(d => d.innerText.slice(0, 80))")
        print("transcript:", said)
        for at, text in page.evaluate("window.__marks"):
            print(f"  {at:6} ms  {text}")
        # End the session and check the microphone really goes
        page.evaluate("""() => { window.__stopped = 0; const o = MediaStreamTrack.prototype.stop;
            MediaStreamTrack.prototype.stop = function () { window.__stopped += 1; return o.call(this); }; }""")
        page.keyboard.press("Escape")
        page.wait_for_selector("[data-testid=mic][aria-pressed=false]", timeout=10_000)
        print("tracks stopped on Esc:", page.evaluate("window.__stopped"))
        context.close()
        browser.close()
