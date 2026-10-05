"""Edit and Retry on something SAID, in a real browser, checked against what the
server stored.

The bug: a spoken message kept the page's own placeholder id (`t7`) — the voice
engines never passed on the stored id the server sends on `routed` and `done` —
so Edit/Retry on it asked the server to cut at an id it had never heard of. The
server cut nothing; the old exchange stayed in Jarvis's memory and came back on
reload. Typed messages always had the swap.

Speech itself cannot be produced here, so the browser's recognition is replaced
by a stand-in that "hears" a sentence (everything after it — the real engine,
its silence detection on a real, silent fake microphone, the real stream, the
real server — is the app's own), and the fake microphone plays a silent file
rather than Chromium's test beep, which never lets the silence watcher fire.
"""

from __future__ import annotations

import wave

import httpx
import pytest

from test_shell_e2e import CHROME, EXPORT, connect_and_select, serve_provider, visit  # noqa: F401

pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

from playwright.sync_api import Page, expect, sync_playwright  # noqa: E402

pytestmark = [
    pytest.mark.skipif(not EXPORT.is_file(), reason="the front end has not been built"),
    pytest.mark.skipif(CHROME is None, reason="no Chromium in this environment"),
]

FAKE_RECOGNITION = """
class FakeRecognition {
  constructor() { this.continuous = false; this.interimResults = false; this.lang = ''; window.__recognition = this; }
  start() { this.running = true; }
  stop() { this.running = false; }
  abort() { this.running = false; }
}
window.SpeechRecognition = FakeRecognition;
window.webkitSpeechRecognition = FakeRecognition;
window.__say = (text) => {
  const result = [{ transcript: text, confidence: 0.97 }];
  result.isFinal = true;
  window.__recognition.onresult({ resultIndex: 0, results: [result] });
};
"""


@pytest.fixture
def silent_wav(tmp_path):
    path = tmp_path / "silence.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000 * 5)
    return path


@pytest.fixture
def speaking_page(live_server, serve_provider, silent_wav):  # noqa: F811
    stub = serve_provider("openai-chat", reply="Hello from the stub.")
    connect_and_select(live_server, stub)
    with sync_playwright() as play:
        browser = play.chromium.launch(executable_path=str(CHROME), args=[
            "--no-sandbox", "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
            f"--use-file-for-fake-audio-capture={silent_wav}"])
        context = browser.new_context(viewport={"width": 1440, "height": 900}, permissions=["microphone"])
        context.add_init_script(FAKE_RECOGNITION)
        page = context.new_page()
        visit(page, live_server)
        yield page, live_server
        context.close()
        browser.close()


def stored_messages(base: str) -> list[tuple[str, str]]:
    listed = httpx.get(f"{base}/api/conversations", timeout=30).json()
    detail = httpx.get(f"{base}/api/conversations/{listed['activeId']}", timeout=30).json()
    return [(m["role"], m["text"]) for m in detail["messages"] if m["role"] in ("user", "assistant")]


def say(page: Page, text: str, replies_expected: int) -> None:
    page.evaluate("(t) => window.__say(t)", text)
    expect(page.locator("text=Hello from the stub.")).to_have_count(replies_expected, timeout=30_000)


def start_listening(page: Page) -> None:
    page.click("[data-testid=settings]")
    page.wait_for_selector("[data-testid=engine-options]")
    page.click("[data-testid=engine-pipeline]")
    page.click("[data-testid=settings]")
    page.click("[data-testid=mic]")
    page.wait_for_function("() => window.__recognition && window.__recognition.running", timeout=15_000)


def message(page: Page, text: str):
    return page.locator("[data-testid=transcript] > div").filter(has_text=text).last


def test_editing_a_spoken_message_really_replaces_it_on_the_server(speaking_page):
    page, base = speaking_page
    start_listening(page)
    say(page, "what does rain on a tin roof sound like", 1)
    page.wait_for_function(
        """() => [...document.querySelectorAll('[data-testid=edit-message]')].length > 0""", timeout=10_000)
    assert stored_messages(base)[0] == ("user", "what does rain on a tin roof sound like")

    page.click("[data-testid=mic]")  # stop listening, then edit what was said
    bubble = message(page, "what does rain on a tin roof sound like")
    bubble.hover()
    bubble.locator("[data-testid=edit-message]").click()
    page.fill("[data-testid=edit-message-input]", "what does a thunderstorm sound like")
    page.click("[data-testid=save-edit]")
    page.wait_for_function(
        """() => !document.querySelector('[data-testid=transcript]').innerText.includes('tin roof')
                 && document.querySelector('[data-testid=transcript]').innerText.includes('thunderstorm')""",
        timeout=15_000)
    expect(page.locator("text=Hello from the stub.")).to_have_count(1, timeout=30_000)
    page.wait_for_timeout(500)

    stored = stored_messages(base)
    assert stored == [("user", "what does a thunderstorm sound like"), ("assistant", "Hello from the stub.")], \
        f"the spoken exchange was not cut on the server: {stored}"
    page.reload(wait_until="load")
    expect(page.locator("[data-testid=transcript]")).to_contain_text("thunderstorm", timeout=15_000)
    assert "tin roof" not in page.inner_text("[data-testid=transcript]")


def test_retrying_a_reply_to_something_said_replaces_it_on_the_server(speaking_page):
    page, base = speaking_page
    start_listening(page)
    say(page, "tell me about forest birdsong", 1)
    page.click("[data-testid=mic]")
    reply = message(page, "Hello from the stub.")
    reply.hover()
    reply.locator("[data-testid=retry-message]").click()
    page.wait_for_timeout(1500)
    expect(page.locator("text=Hello from the stub.")).to_have_count(1, timeout=30_000)
    stored = stored_messages(base)
    assert stored == [("user", "tell me about forest birdsong"), ("assistant", "Hello from the stub.")], \
        f"Retry left the first exchange on the server: {stored}"
