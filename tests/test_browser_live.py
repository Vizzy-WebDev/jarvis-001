"""Jarvis's own browser, for real: the real model, a real Chrome or Edge, real websites.

NOT part of the ordinary suite. It spends the person's model quota and needs the internet, so
it runs only when asked:

    JARVIS_LIVE_TESTS=1 python -m pytest tests/test_browser_live.py -q

Each test sends the sort of message a person would, through the real chat stream of a real
server (`live_harness.py`), and then checks what REALLY happened - the browser window's actual
page, the files that were kept, the stored tool calls - rather than what the model said it did.
The model's own words are only checked loosely (it paraphrases); the state of the world is
checked exactly.

A site can change. Where a failure is a website's doing (down, redesigned, or blocking), the
test says which site, so it can be told apart from Jarvis's.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import httpx
import pytest
from live_harness import REAL_ENV, REAL_MODELS, LiveApp

from jarvis.browser.locate import find_browser

pytestmark = [
    pytest.mark.skipif(os.environ.get("JARVIS_LIVE_TESTS") != "1",
                       reason="live tests spend model quota: set JARVIS_LIVE_TESTS=1"),
    pytest.mark.skipif(not (REAL_ENV.exists() and REAL_MODELS.exists()),
                       reason="needs the person's own .env and models.yaml to read"),
    pytest.mark.skipif(find_browser() is None, reason="no Chrome or Edge to drive"),
]

CLICKS = {"browser_click", "browser_type"}


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    app = LiveApp(tmp_path_factory.mktemp("live") / "data")
    app.start()
    yield app
    print("\nBROWSER TOOL BATCHING:", app.batching())
    if os.environ.get("JARVIS_LIVE_KEEP"):
        app.keep_evidence(Path(os.environ["JARVIS_LIVE_KEEP"]))
    app.stop()


def lower(text: str) -> str:
    return (text or "").lower()


# --- reading ---------------------------------------------------------------------------

def test_a_real_page_is_opened_and_read(live):
    live.new_chat()
    mark = live.mark()
    reply = live.say("Go to https://example.com and tell me in one sentence what the page says.")
    assert reply.failed is None, reply.failed
    assert reply.used("browser_navigate")
    opened = live.results_of("browser_navigate", mark)[-1]
    assert opened["ok"] is True and opened["title"] == "Example Domain"
    assert "documentation" in lower(opened["text"])
    assert opened["ui_action"]["kind"] == "browser", "the chat gets no card for the window"
    assert any(a.get("kind") == "browser" for a in reply.attachments)
    assert "example" in lower(reply.text)
    assert live.window()["status"] in {"active", "closed"}


def test_a_long_real_page_is_cut_for_the_model_and_still_answers_the_question(live):
    live.new_chat()
    mark = live.mark()
    reply = live.say("Go to https://en.wikipedia.org/wiki/Alan_Turing and tell me his birth "
                     "date and where he was born.")
    assert reply.failed is None, reply.failed
    opened = live.results_of("browser_navigate", mark)[-1]
    assert opened["ok"] is True, opened
    assert opened.get("textTruncated") is True, "a page this long must be cut for the model"
    assert len(opened["text"]) <= 20000
    assert "1912" in reply.text, reply.text


# --- acting, with the person's go-ahead ----------------------------------------------------

def test_clicking_asks_first_then_follows_a_real_link(live):
    live.new_chat()
    reply = live.say("Go to https://example.com, click the Learn more link, then tell me the "
                     "title of the page you end up on.", allow=CLICKS, max_follow_ups=3)
    assert reply.failed is None, reply.failed
    assert any(name == "browser_click" and not ok and "needs confirming" in (error or "")
               for name, ok, error in reply.tools), \
        "the click ran without being allowed first"
    assert ("browser_click", "allow") in reply.approvals
    assert live.window()["url"].startswith("https://www.iana.org/help/example-domains")
    assert "example domains" in lower(reply.text)
    # Whether the model sends a click together with another browser call is behaviour of the
    # model, not a fault of the browser, and it varies from run to run. The outcome above is
    # what is asserted; how often it happens is reported (see BROWSER TOOL BATCHING).
    print("\nbatching so far:", live.batching())


def test_a_click_the_person_refuses_does_not_happen(live):
    live.new_chat()
    live.say("Go to https://example.com.")
    reply = live.say("Now click the Learn more link.", deny=CLICKS)
    assert ("browser_click", "deny") in reply.approvals
    assert live.window()["url"].rstrip("/") == "https://example.com", "it clicked anyway"


def test_typing_into_a_real_form_and_submitting_it(live):
    live.new_chat()
    mark = live.mark()
    reply = live.say(
        "In your browser, go to https://httpbin.org/forms/post. Type the name Ada Lovelace into "
        "the customer name field and submit the order.", allow=CLICKS, max_follow_ups=5)
    assert reply.failed is None, reply.failed
    # What the site really answered, from what the browser handed back (the model may well have
    # closed the window since): the order page the site echoes back, with the name in it.
    seen = [page for name in ("browser_type", "browser_click", "browser_read_page")
            for page in live.results_of(name, mark) if page.get("ok")]
    echoed = [page for page in seen if str(page.get("url", "")).startswith("https://httpbin.org/post")]
    assert echoed, f"the form never reached the site; pages seen: {[p.get('url') for p in seen]}"
    assert any("Ada Lovelace" in page["text"] for page in echoed),         "what was typed never reached the site"


# --- sessions that last --------------------------------------------------------------------

def test_a_sign_in_survives_closing_the_window_and_restarting_jarvis(live):
    live.new_chat()
    mark = live.mark()
    live.say("In your browser, go to https://httpbin.org/cookies/set?jarvis=remembered and tell "
             "me what cookies the page shows.")
    assert "remembered" in live.results_of("browser_navigate", mark)[-1]["text"]

    live.new_chat()
    closed = live.say("Close the browser window.")
    assert closed.used("browser_close")
    assert live.window()["running"] is False
    assert live.window()["status"] == "closed"

    live.restart()                                  # Jarvis itself quits and starts again
    live.new_chat()
    mark = live.mark()
    reply = live.say("In your browser, go to https://httpbin.org/cookies and tell me exactly "
                     "which cookies it shows.")
    shown = live.results_of("browser_navigate", mark)[-1]["text"]
    assert "remembered" in shown, f"the cookie did not survive: {shown!r}"
    assert "remembered" in lower(reply.text)


def test_a_sign_in_survives_jarvis_being_killed_without_closing_anything(live):
    live.new_chat()
    mark = live.mark()
    live.say("In your browser, go to https://httpbin.org/cookies/set?second=also-kept and tell "
             "me what cookies the page shows.")
    assert "also-kept" in live.results_of("browser_navigate", mark)[-1]["text"]
    live.process_kill()                             # the console window closed: no goodbye
    live.start_again()
    live.new_chat()
    mark = live.mark()
    live.say("In your browser, go to https://httpbin.org/cookies and tell me exactly which "
             "cookies it shows.")
    text = live.results_of("browser_navigate", mark)[-1]["text"]
    assert "also-kept" in text, f"a hard exit lost the sign-in: {text!r}"
    assert "remembered" in text


def test_the_person_can_fill_in_a_real_form_by_hand_through_take_control(live):
    """No model at all: the same requests the Take control page sends, against a real site.
    Tab focuses the form's first field, the text goes in, Enter submits it."""
    opened = httpx.post(f"{live.base}/api/browsers", json={"url": "https://httpbin.org/forms/post"},
                        timeout=90)
    assert opened.status_code == 201, opened.text
    window = opened.json()["session"]["id"]
    send = lambda body: httpx.post(f"{live.base}/api/browsers/{window}/input", json=body,  # noqa: E731
                                   timeout=60)
    assert send({"type": "key", "key": "Tab"}).json()["ok"] is True
    assert send({"type": "text", "text": "Grace Hopper"}).json()["ok"] is True
    assert send({"type": "key", "key": "Enter"}).json()["ok"] is True
    deadline = time.time() + 20
    page: dict = {}
    while time.time() < deadline:
        page = httpx.get(f"{live.base}/api/browsers/{window}/read", timeout=60).json()
        if str(page.get("url", "")).startswith("https://httpbin.org/post"):
            break
        time.sleep(0.5)
    assert "Grace Hopper" in page.get("text", ""), f"what was typed by hand never arrived: {page}"
    httpx.post(f"{live.base}/api/browsers/{window}/close", timeout=60)


# --- PDFs ----------------------------------------------------------------------------------

def test_a_pdf_at_an_address_is_caught_and_kept(live):
    live.new_chat()
    mark = live.mark()
    reply = live.say("In your browser, go to https://www.w3.org/WAI/ER/tests/xhtml/testfiles/"
                     "resources/pdf/dummy.pdf and keep the PDF for me.")
    assert reply.failed is None, reply.failed
    assert reply.used("browser_save_downloads")
    saved = live.results_of("browser_save_downloads", mark)[-1]
    assert saved["ok"] is True and [f["name"] for f in saved["saved"]] == ["dummy.pdf"], saved
    kept = [a for a in live.artifacts() if a["name"] == "dummy.pdf"]
    assert len(kept) == 1 and kept[0]["kind"] == "pdf"
    body = httpx.get(f"{live.base}{kept[0]['url']}", timeout=30)
    assert body.content.startswith(b"%PDF-") and len(body.content) > 5000
    assert any(a.get("artifactId") == kept[0]["id"] for a in reply.attachments), \
        "the chat shows no file card for it"


def test_a_pdf_behind_a_link_is_found_clicked_and_kept(live):
    live.new_chat()
    reply = live.say("Go to https://www.irs.gov/forms-pubs/about-form-1040, click the link to "
                     "the Form 1040 PDF, and keep the PDF for me.", allow=CLICKS,
                     max_follow_ups=5)
    assert reply.failed is None, reply.failed
    kept = [a for a in live.artifacts() if a["name"].lower().startswith("f1040")]
    assert kept, f"no Form 1040 PDF was kept; saw {[a['name'] for a in live.artifacts()]}"
    body = httpx.get(f"{live.base}{kept[0]['url']}", timeout=60)
    assert body.content.startswith(b"%PDF-") and len(body.content) > 100_000


def test_a_download_that_is_not_a_pdf_is_refused_in_plain_words(live):
    live.new_chat()
    url = ("https://httpbin.org/response-headers?Content-Type=text%2Fcsv&"
           "Content-Disposition=attachment%3B%20filename%3Ddata.csv")
    mark = live.mark()
    reply = live.say(f"In your browser, go to {url} and keep whatever file it downloads.")
    assert reply.failed is None, reply.failed
    seen = (live.results_of("browser_navigate", mark) + live.results_of("browser_read_page", mark)
            + live.results_of("browser_save_downloads", mark))
    problems = [r.get("downloadProblems") for r in seen if r.get("downloadProblems")]
    assert problems and "Only PDF downloads can be imported" in str(problems), problems
    assert not [a for a in live.artifacts() if a["name"] == "data.csv"]


# --- going wrong, plainly ------------------------------------------------------------------

def test_a_site_that_does_not_exist_is_a_plain_answer(live):
    live.new_chat()
    mark = live.mark()
    reply = live.say("In your browser, go to https://this-domain-does-not-exist-9z8y7.example "
                     "and tell me what is there.")
    assert reply.failed is None, reply.failed
    assert reply.used("browser_navigate"), "the model never tried the browser"
    result = live.results_of("browser_navigate", mark)[-1]
    assert result["ok"] is False and result["code"] in {"DNS_UNAVAILABLE", "NAVIGATION_FAILED"}
    assert "Traceback" not in reply.text and reply.text.strip()


def test_jarvis_own_address_and_the_local_network_are_refused(live):
    for target in (f"{live.base}/api/status", "http://localhost:3000", "http://192.168.1.1"):
        live.new_chat()          # a fresh chat each time: in one, the model learns and refuses unasked
        mark = live.mark()
        reply = live.say(f"In your browser, go to {target} and tell me what is there.")
        assert reply.failed is None, reply.failed
        assert reply.used("browser_navigate"), f"the model never tried the browser for {target}"
        result = live.results_of("browser_navigate", mark)[-1]
        assert result["ok"] is False and result["code"] == "BLOCKED_URL", (target, result)
        assert "off limits" in result["error"]


def test_acting_with_no_window_open_says_so(live):
    live.new_chat()
    live.say("Close the browser window.")
    reply = live.say("Click the Submit button on the page.", allow=CLICKS)
    assert reply.failed is None, reply.failed
    assert reply.text.strip()
    errors = [r for r in live.results_of("browser_click")[-1:] if r.get("ok") is False]
    assert not errors or "browser_navigate first" in errors[-1]["error"]
