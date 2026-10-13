"""Edit and Retry on something SAID, in a real browser, checked against what the
server stored.

The bug: a spoken message kept the page's own placeholder id (`t7`) — the voice
engines never passed on the stored id the server sends on `routed` and `done` —
so Edit/Retry on it asked the server to cut at an id it had never heard of. The
server cut nothing; the old exchange stayed in Jarvis's memory and came back on
reload. Typed messages always had the swap.

Speech itself cannot be produced here, so the server's recogniser is replaced by a
stand-in session that "hears" a sentence when the test says so. Everything around it is
the app's own: the real engine streaming real microphone frames over the real recognition
socket, its silence detection on a real, silent fake microphone, the real chat stream, the
real server. The fake microphone plays a silent file rather than Chromium's test beep,
which never lets the silence watcher fire.
"""

from __future__ import annotations

import asyncio
import threading
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

class Ears:
    """The stand-in recogniser sessions the server opens, and a way to make them hear."""

    def __init__(self) -> None:
        self.sessions: list = []
        self.frames = 0
        self.lock = threading.Lock()

    def hear(self, text: str) -> None:
        with self.lock:
            session = self.sessions[-1]
        session.loop.call_soon_threadsafe(session.events.put_nowait, {
            "type": "transcript", "text": text, "isFinal": True, "speechFinal": True, "confidence": 0.97})


@pytest.fixture
def ears(monkeypatch):
    from jarvis import speech_models
    from jarvis.stt import local

    heard = Ears()

    class StandIn:
        def __init__(self, model_id: str = local.DEFAULT_MODEL) -> None:
            self.loop = asyncio.get_running_loop()
            self.events: asyncio.Queue = asyncio.Queue()
            with heard.lock:
                heard.sessions.append(self)

        async def warm(self) -> None:
            pass

        async def send(self, pcm: bytes) -> None:
            heard.frames += 1          # real microphone frames really arrive

        def __aiter__(self):
            return self

        async def __anext__(self):
            event = await self.events.get()
            if event is None:
                raise StopAsyncIteration
            return event

        async def close(self) -> None:
            self.events.put_nowait(None)

    monkeypatch.setattr(local, "available", lambda: True)
    monkeypatch.setattr(local, "LocalSession", StandIn)
    monkeypatch.setattr(speech_models, "ensure",
                        lambda model_id: {"id": model_id, "label": model_id, "state": "ready",
                                          "done": 1, "total": 1, "error": None})
    return heard


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
def speaking_page(live_server, serve_provider, silent_wav, ears):  # noqa: F811
    stub = serve_provider("openai-chat", reply="Hello from the stub.")
    connect_and_select(live_server, stub)
    with sync_playwright() as play:
        browser = play.chromium.launch(executable_path=str(CHROME), args=[
            "--no-sandbox", "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
            f"--use-file-for-fake-audio-capture={silent_wav}"])
        context = browser.new_context(viewport={"width": 1440, "height": 900}, permissions=["microphone"])
        page = context.new_page()
        page.__ears = ears
        visit(page, live_server)
        yield page, live_server
        context.close()
        browser.close()


def stored_messages(base: str) -> list[tuple[str, str]]:
    listed = httpx.get(f"{base}/api/conversations", timeout=30).json()
    detail = httpx.get(f"{base}/api/conversations/{listed['activeId']}", timeout=30).json()
    return [(m["role"], m["text"]) for m in detail["messages"] if m["role"] in ("user", "assistant")]


def say(page: Page, text: str, replies_expected: int) -> None:
    page.__ears.hear(text)
    # Generous: the first turn of a test process pays for loading the whole turn loop, and on a
    # busy laptop that alone has taken over half a minute.
    expect(page.locator("text=Hello from the stub.")).to_have_count(replies_expected, timeout=120_000)


def start_listening(page: Page) -> None:
    page.click("[data-testid=settings]")
    page.click("[data-testid=listening-select]")
    page.click("[data-testid=engine-pipeline]")
    page.keyboard.press("Escape")
    page.click("[data-testid=mic]")
    page.wait_for_function("() => document.querySelector('[data-testid=mic]').getAttribute('aria-pressed') === 'true'",
                           timeout=15_000)
    # Listening for real: the microphone's frames are reaching the server's recogniser.
    deadline = page.evaluate("Date.now()") + 15_000
    while page.__ears.frames < 5:
        assert page.evaluate("Date.now()") < deadline, "no microphone frames reached the server"
        page.wait_for_timeout(100)


def message(page: Page, text: str):
    return page.locator("[data-testid=transcript] > div").filter(has_text=text).last


def test_editing_a_spoken_message_really_replaces_it_on_the_server(speaking_page):
    page, base = speaking_page
    start_listening(page)
    say(page, "what does rain on a tin roof sound like", 1)
    page.wait_for_function(
        """() => [...document.querySelectorAll('[data-testid=edit-message]')].length > 0""", timeout=10_000)
    assert stored_messages(base)[0] == ("user", "what does rain on a tin roof sound like")

    # End the session (Esc; the mic only mutes during one), then edit what was said.
    page.keyboard.press("Escape")
    page.wait_for_function("() => document.querySelector('[data-testid=mic]').getAttribute('aria-pressed') === 'false'")
    bubble = message(page, "what does rain on a tin roof sound like")
    bubble.hover()
    bubble.locator("[data-testid=edit-message]").click()
    # Edit loads the message into the composer (design 1h), tagged as an edit.
    page.wait_for_selector("[data-testid=editing-tag]")
    page.fill("[data-testid=composer-input]", "what does a thunderstorm sound like")
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
