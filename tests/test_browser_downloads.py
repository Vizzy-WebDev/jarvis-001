"""PDFs from the web: caught by the browser, refused when they are not PDFs, and kept as
files Jarvis holds - by the agent, or by the person from the chat card.

Real Chromium against the fixture site. The claims: a PDF a click or a link brings down
is caught without anyone asking; only PDFs are kept, whatever the site called them;
size and count are limited; a download is never kept twice; and a kept PDF is an
ordinary artifact that belongs to the chat that asked for it.
"""

from __future__ import annotations

import json
import time

import pytest
from browser_site import BASE, FixtureSite
from fastapi.testclient import TestClient

from jarvis import assembly
from jarvis.artifacts import get as get_artifact
from jarvis.browser import set_host_for_tests
from jarvis.browser import downloads as downloads_store
from jarvis.browser.locate import find_browser
from jarvis.connectors import browser_connector, store
from jarvis.db import reset_for_tests as reset_db
from jarvis.main import create_app
from jarvis.orchestrator.pipeline import _attachments_of

needs_browser = pytest.mark.skipif(find_browser() is None, reason="no Chrome or Edge to drive")

WINDOW = browser_connector.AGENT_SESSION_ID


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    assembly.reset_for_tests()
    yield
    set_host_for_tests(None)
    assembly.reset_for_tests()
    reset_db()


@pytest.fixture
def site(tmp_path):
    fixture = FixtureSite()
    host = fixture.host(tmp_path / "sessions")
    set_host_for_tests(host)
    yield fixture
    fixture.close()


def go(url: str) -> dict:
    return browser_connector.dispatch("browser_navigate", {"url": url}, {})


def click(label: str) -> dict:
    page = browser_connector.dispatch("browser_read_page", {}, {})
    selector = next(e["selector"] for e in page["elements"] if e["label"] == label)
    return browser_connector.dispatch("browser_click", {"selector": selector}, {})


def save(session_id: str | None = None) -> dict:
    return browser_connector.dispatch("browser_save_downloads", {}, {}, session_id=session_id)


# --- the files themselves, without a browser ----------------------------------------

def test_a_download_is_found_only_by_an_id_this_module_made(tmp_path):
    (tmp_path / "downloads").mkdir()
    (tmp_path / "downloads" / "secret.pdf").write_bytes(b"%PDF-")
    for hostile in ("../x", "..\\x", "secret", "", "C:/Windows/win.ini"):
        assert downloads_store.download_path(tmp_path, hostile) is None


def test_a_transfer_a_restart_interrupted_becomes_a_recorded_failure(tmp_path):
    outcomes = tmp_path / "download-outcomes"
    outcomes.mkdir()
    (tmp_path / "downloads").mkdir()
    interrupted = "0a7e5d11-6a72-4a76-8a1c-00000000dd01"
    (outcomes / f"{interrupted}.json").write_text(json.dumps({
        "id": interrupted, "name": "half.pdf", "status": "pending",
        "code": "DOWNLOAD_INTERRUPTED", "message": "interrupted", "createdAt": "2026-10-11T00:00:00Z"}))
    (tmp_path / "downloads" / f"{interrupted}.pdf").write_bytes(b"%PDF-1.4 half")
    assert downloads_store.read_failures(tmp_path) == []          # pending is not yet a failure
    recovered = downloads_store.read_failures(tmp_path, recover_interrupted=True)
    assert [f["name"] for f in recovered] == ["half.pdf"]
    assert not (tmp_path / "downloads" / f"{interrupted}.pdf").exists(), "a partial file was kept"


# --- from a page ---------------------------------------------------------------------

@needs_browser
def test_a_click_that_downloads_a_pdf_says_so_and_the_agent_can_keep_it(site):
    store.get_or_create_singleton("browser")
    go(f"{BASE}/downloads")
    result = click("Download the report")
    assert result["ok"] is True and result["url"] == f"{BASE}/downloads", "the page stayed put"
    assert [d["name"] for d in result["downloads"]] == ["report.pdf"]
    assert "browser_save_downloads" in result["downloadNote"]

    saved = save()
    assert saved["ok"] is True
    (file,) = saved["saved"]
    assert file["name"] == "report.pdf" and file["url"].startswith("/api/artifacts/")

    artifact = get_artifact(file["id"])
    assert artifact is not None and artifact.path.read_bytes().startswith(b"%PDF-")
    assert artifact.kind == "pdf" and artifact.mime_type == "application/pdf"
    assert artifact.verified is None, "a downloaded file must never be recorded as verified"
    assert "website" in (artifact.detail or "")

    # The chat shows it like anything else Jarvis keeps: a file card for an artifact.
    (card,) = _attachments_of(saved)
    assert card["artifactId"] == file["id"] and card["kind"] == "file"

    # Once kept it is no longer "waiting".
    after = browser_connector.dispatch("browser_read_page", {}, {})
    assert "downloads" not in after and "downloadNote" not in after


@needs_browser
def test_keeping_the_same_download_again_returns_the_same_file(site):
    go(f"{BASE}/downloads")
    click("Download the report")
    first = save()
    again = save()
    assert [f["id"] for f in again["saved"]] == [f["id"] for f in first["saved"]]
    assert "ui_action" not in again, "a card was shown a second time for the same file"
    from jarvis.artifacts import list_page

    assert len(list_page()[0]) == 1


@needs_browser
def test_a_pdf_the_address_itself_points_at_is_caught_whether_or_not_it_is_marked_a_download(site):
    """Most real report and invoice links are served inline, which a browser would show in
    its own viewer: an empty page to read and nothing to keep. Both kinds must arrive."""
    go(f"{BASE}/two")
    for path in ("report.pdf", "inline.pdf"):
        opened = go(f"{BASE}/{path}")
        assert opened["ok"] is True, path
        assert opened["url"] == f"{BASE}/two", f"{path} replaced the page with a viewer"
    names = sorted(f["name"] for f in save()["saved"])
    assert names == ["inline.pdf", "report.pdf"]


@needs_browser
def test_a_file_that_is_not_a_pdf_is_refused_in_plain_words(site):
    go(f"{BASE}/downloads")
    result = click("Download the data")
    deadline = time.time() + 10
    while "downloadProblems" not in result and time.time() < deadline:
        time.sleep(0.3)
        result = browser_connector.dispatch("browser_read_page", {}, {})
    assert result["downloadProblems"] == [
        "Only PDF downloads can be imported. This file was not a PDF."]
    assert "downloads" not in result
    assert save()["saved"] == []


@needs_browser
def test_a_file_called_pdf_that_is_not_one_is_refused(site):
    go(f"{BASE}/downloads")
    click("Download the fake")
    assert save()["saved"] == []


@needs_browser
def test_a_download_over_the_size_limit_is_stopped_and_reported(site):
    go(f"{BASE}/downloads")
    result = click("Download the huge one")
    deadline = time.time() + 15
    while "downloadProblems" not in result and time.time() < deadline:
        time.sleep(0.3)
        result = browser_connector.dispatch("browser_read_page", {}, {})
    assert result["downloadProblems"] == ["The download is over 10 MB. Choose a smaller PDF."]
    assert save()["saved"] == []


@needs_browser
def test_nothing_waiting_is_a_plain_answer_not_an_error(site):
    go(f"{BASE}/two")
    nothing = save()
    assert nothing["ok"] is True and nothing["saved"] == []
    assert nothing["note"] == "There is no downloaded PDF waiting."


def test_saving_with_no_browser_ever_opened_starts_nothing(tmp_path):
    from jarvis.browser import BrowserHost

    host = BrowserHost(directory=tmp_path / "s", find=lambda: None)
    result = browser_connector.dispatch("browser_save_downloads", {}, {}, host=host)
    assert result == {"ok": True, "saved": [], "note": "There is no downloaded PDF waiting."}
    assert not host.started


@needs_browser
def test_a_saved_pdf_belongs_to_the_chat_that_asked_for_it(site, scratch):
    """The conversation id comes from the call's own session, so the Artifacts page can
    take the person back to the chat that made the file."""
    from jarvis import conversation, session

    conversation.reset_for_tests()
    session.reset_for_tests()
    chat = session.get_active_session_id()
    from jarvis.db import get_db

    get_db().execute("INSERT OR IGNORE INTO conversations (id, title, created_at, updated_at) "
                     "VALUES (?, 'A chat', '2026-10-11T00:00:00Z', '2026-10-11T00:00:00Z')", (chat,))
    go(f"{BASE}/downloads")
    click("Download the report")
    (file,) = save(session_id=chat)["saved"]
    assert get_artifact(file["id"]).conversation_id == chat


# --- from the person, over HTTP --------------------------------------------------------

@needs_browser
def test_the_person_can_keep_what_a_window_caught_from_the_card(site):
    client = TestClient(create_app())
    go(f"{BASE}/downloads")
    click("Download the report")

    info = client.get(f"/api/browsers/{WINDOW}").json()
    assert info["downloads"] == 1
    listing = client.get(f"/api/browsers/{WINDOW}/downloads").json()
    assert [d["name"] for d in listing["downloads"]] == ["report.pdf"]

    kept = client.post(f"/api/browsers/{WINDOW}/import-downloads").json()
    assert kept["ok"] is True and len(kept["created"]) == 1
    assert client.get(f"/api/browsers/{WINDOW}").json()["downloads"] == 0

    again = client.post(f"/api/browsers/{WINDOW}/import-downloads").json()
    assert again["created"] == [] and [f["id"] for f in again["files"]] == [
        f["id"] for f in kept["files"]]
    # And it is a real artifact, served as a download like every other.
    served = client.get(kept["files"][0]["url"])
    assert served.status_code == 200 and served.content.startswith(b"%PDF-")
    assert served.headers["content-disposition"].startswith("attachment")
