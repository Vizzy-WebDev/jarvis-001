"""Files attached in conversation: which route each one takes, and why.

The interesting cases are the refusals and the honest notes. A file that cannot
be read has to say so — an attachment silently contributing nothing is how a
model ends up answering about a document it never saw.
"""

from __future__ import annotations

import base64

import pytest

from jarvis import conversation, uploads
from jarvis.attachments import compose_message, prepare_for_turn
from jarvis.background import join_all
from jarvis.db import reset_for_tests as reset_db

#: A real 1x1 PNG, so the image path is exercised on actual image bytes.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    conversation.reset_for_tests()
    yield
    join_all()
    conversation.reset_for_tests()
    reset_db()


def _attach(name: str, data: bytes | str) -> str:
    payload = data.encode("utf-8") if isinstance(data, str) else data
    return uploads.save_upload(payload, name)["id"]


# --- the upload store ---------------------------------------------------------

def test_an_id_that_tries_to_escape_the_folder_resolves_to_nothing():
    for hostile in ("../../.env", "..\\..\\.env", "/etc/passwd", "", "a/b"):
        assert uploads.get_upload(hostile) is None


def test_a_hostile_filename_is_flattened_rather_than_obeyed():
    saved = uploads.save_upload(b"x", "../../.env")
    assert "/" not in saved["id"] and "\\" not in saved["id"]
    assert saved["id"].endswith("env")
    assert uploads.get_upload(saved["id"])["path"].startswith(str(uploads.upload_dir()))


def test_an_empty_upload_fails_loudly_rather_than_leaving_a_zero_byte_file():
    with pytest.raises(ValueError):
        uploads.save_upload(b"", "empty.txt")


def test_an_id_survives_being_handed_back_later():
    upload_id = _attach("notes.txt", "hello")
    assert uploads.get_upload(upload_id)["name"] == "notes.txt"


def test_old_uploads_are_pruned_but_recent_ones_are_kept(monkeypatch):
    keep = _attach("keep.txt", "recent")
    stale = _attach("stale.txt", "old")
    import os
    import time

    old = time.time() - (uploads.MAX_AGE_S + 60)
    os.utime(uploads.get_upload(stale)["path"], (old, old))
    uploads.prune()
    assert uploads.get_upload(stale) is None
    assert uploads.get_upload(keep) is not None


# --- which route a file takes -------------------------------------------------

def test_an_image_rides_inside_the_message_and_requires_a_model_that_can_see():
    prepared = prepare_for_turn([_attach("photo.png", PNG)], session_id="s1")
    assert prepared.need == {"vision": True}
    assert prepared.media[0]["kind"] == "image"
    assert prepared.media[0]["mimeType"] == "image/png"
    assert prepared.text_blocks == []


def test_a_text_document_is_read_into_the_message_and_needs_no_capability():
    prepared = prepare_for_turn([_attach("notes.md", "# Title\n\nThe body.")], session_id="s1")
    assert prepared.need == {}
    assert "The body." in prepared.text_blocks[0]
    assert "notes.md" in prepared.text_blocks[0]


def test_a_long_document_is_truncated_and_says_so():
    prepared = prepare_for_turn([_attach("big.txt", "x" * 60000)], session_id="s1")
    assert "truncated — the file is longer than this" in prepared.text_blocks[0]


def test_a_code_file_nobody_listed_is_still_read_by_looking_at_the_bytes():
    """The alternative is an extension list that is never finished, and a .kt
    file read as binary because nobody thought of it."""
    prepared = prepare_for_turn([_attach("script.kt", "fun main() { }")], session_id="s1")
    assert "fun main()" in prepared.text_blocks[0]


def test_something_genuinely_binary_is_refused_rather_than_pasted_as_mojibake():
    prepared = prepare_for_turn([_attach("thing.bin", bytes(range(256)) * 40)],
                                session_id="s1")
    assert prepared.text_blocks == []
    assert "isn't a kind of file that can be read" in prepared.notes[0]


def test_an_office_document_is_read_as_structure_not_as_bytes(tmp_path):
    from jarvis.artifacts import office as writer

    path = tmp_path / "report.docx"
    writer.write_docx(path, ["Quarterly figures", "Revenue was up."])
    prepared = prepare_for_turn([_attach("report.docx", path.read_bytes())], session_id="s1")
    assert "Revenue was up." in prepared.text_blocks[0]
    assert prepared.need == {}, "reading it as text needs no capability at all"


def test_a_big_workbook_points_at_the_tool_that_can_answer_over_all_of_it(tmp_path):
    from jarvis.artifacts import office as writer

    path = tmp_path / "big.xlsx"
    writer.write_xlsx(path, [["n"], *[[str(i)] for i in range(500)]])
    upload_id = _attach("big.xlsx", path.read_bytes())
    prepared = prepare_for_turn([upload_id], session_id="s1")

    note = " ".join(prepared.notes)
    assert "analyze_spreadsheet" in note
    assert upload_id in note, "the tool needs the real id, not the filename"


def test_a_missing_attachment_is_reported_rather_than_ignored():
    prepared = prepare_for_turn(["nope"], session_id="s1")
    assert "could not be found any more" in prepared.notes[0]


def test_a_pdf_with_no_capable_model_says_so_instead_of_guessing(monkeypatch):
    monkeypatch.setattr("jarvis.gateway.routing.build_candidates", lambda *a, **k: [])
    prepared = prepare_for_turn([_attach("paper.pdf", b"%PDF-1.4 nonsense")], session_id="s1")
    assert prepared.media == []
    assert "none of the available models can read one directly" in prepared.notes[0]


def test_a_pdf_with_a_capable_model_rides_along(monkeypatch):
    monkeypatch.setattr("jarvis.gateway.routing.build_candidates",
                        lambda *a, **k: [{"id": "gem"}])
    prepared = prepare_for_turn([_attach("paper.pdf", b"%PDF-1.4 nonsense")], session_id="s1")
    assert prepared.media[0]["mimeType"] == "application/pdf"
    assert prepared.need == {"video": True}


def test_a_video_is_registered_and_explicitly_not_watched():
    """Auto-ingesting on arrival is exactly the "analyse before being asked"
    behaviour the whole content design exists to prevent."""
    prepared = prepare_for_turn([_attach("clip.mp4", b"\x00\x00\x00\x18ftypmp42")],
                                session_id="s1")
    join_all()
    note = prepared.notes[0]
    assert "NOT been read or watched" in note
    assert "reference id: ct" in note
    assert prepared.media == [], "a video never rides inside the message"

    from jarvis.content import store

    assert store.latest_in_session("s1")["identity"]["kind"] == "video"


# --- composing the message ----------------------------------------------------

def test_documents_and_notes_are_folded_into_the_users_own_message():
    prepared = prepare_for_turn([_attach("notes.txt", "the body")], session_id="s1")
    composed = compose_message("what does this say?", prepared)
    assert composed.endswith("what does this say?")
    assert "the body" in composed


def test_a_note_is_marked_as_not_something_the_user_said():
    prepared = prepare_for_turn(["missing"], session_id="s1")
    composed = compose_message("hello", prepared)
    assert "[Note for you, not spoken by the user:" in composed


def test_no_attachments_leaves_the_message_exactly_as_it_was():
    assert compose_message("just words", None) == "just words"


# --- through a real turn ------------------------------------------------------

def test_an_attached_image_reaches_the_transcript_and_the_model_requirement():
    from jarvis.capabilities import CapabilityRegistry
    from jarvis.events.bus import EventBus
    from jarvis.orchestrator import Orchestrator, TurnRequest
    from jarvis.orchestrator.model_port import StepComplete

    seen: dict[str, object] = {}

    class Model:
        def stream(self, *, messages, system, tools, session_id, need=None):
            seen["need"] = need
            seen["media"] = messages[-1].get("media")
            yield StepComplete(text="A cat.", model_id="stub")

    upload_id = _attach("photo.png", PNG)
    orchestrator = Orchestrator(Model(), registry=CapabilityRegistry(), event_bus=EventBus())
    list(orchestrator.run_turn(TurnRequest(text="what is this?", session_id="s1",
                                           attachments=(upload_id,))))

    assert seen["need"] == {"vision": True}
    assert seen["media"][0]["kind"] == "image"
    stored = conversation.get_messages("s1")[0]
    assert stored["role"] == "user" and stored["media"][0]["kind"] == "image"


def test_a_turn_with_only_an_attachment_and_no_words_is_not_rejected(live_server):
    import httpx

    upload_id = _attach("notes.txt", "hello")
    with httpx.Client(base_url=live_server, timeout=10.0) as client:
        response = client.post("/api/uploads", params={"name": "notes.txt"}, content=b"hello")
        assert response.json()["ok"] is True
        # An empty message with an attachment is an ordinary thing to send.
        streamed = client.get("/api/chat/stream",
                              params={"message": "", "attachments": upload_id})
    assert streamed.status_code == 200


def test_an_upload_round_trips_through_the_route(live_server):
    import httpx

    with httpx.Client(base_url=live_server, timeout=10.0) as client:
        saved = client.post("/api/uploads", params={"name": "notes.txt"},
                            content=b"hello").json()
        described = client.get(f"/api/uploads/{saved['id']}").json()
        missing = client.get("/api/uploads/nope")

    assert described["name"] == "notes.txt" and described["size"] == 5
    assert "path" not in described, "a path must never go back to the browser"
    assert missing.status_code == 404


# --- analyze_spreadsheet ------------------------------------------------------

def test_analysing_a_workbook_runs_a_real_script_over_the_real_file(monkeypatch, tmp_path):
    from jarvis.artifacts import office as writer
    from jarvis.gateway.client import Answer
    from jarvis.tools.analyze_spreadsheet import SPEC

    path = tmp_path / "sales.xlsx"
    writer.write_xlsx(path, [["item", "amount"], *[["thing", str(i)] for i in range(1, 101)]])
    upload_id = _attach("sales.xlsx", path.read_bytes())

    script = ("import csv\n"
              "rows = list(csv.DictReader(open('data.csv')))\n"
              "print('The total is', sum(int(r['amount']) for r in rows))\n")
    monkeypatch.setattr("jarvis.gateway.client.ask",
                        lambda *a, **k: Answer(text=f"```python\n{script}```", model_id="stub"))

    answer = SPEC.handler(upload_id=upload_id, question="what is the total?")
    assert answer["ok"] is True
    # 1..100 — computed over every row, not the five-row sample in the prompt.
    assert answer["answer"] == "The total is 5050"
    assert answer["rowCount"] == 100
    assert "csv.DictReader" in answer["script"], "the script is shown, not hidden"


def test_a_failing_script_is_fixed_once_and_then_reported_honestly(monkeypatch, tmp_path):
    from jarvis.artifacts import office as writer
    from jarvis.gateway.client import Answer
    from jarvis.tools.analyze_spreadsheet import SPEC

    path = tmp_path / "sales.xlsx"
    writer.write_xlsx(path, [["n"], ["1"], ["2"]])
    upload_id = _attach("sales.xlsx", path.read_bytes())

    attempts = {"count": 0}

    def fake_ask(prompt, **kw):
        attempts["count"] += 1
        if attempts["count"] == 1:
            return Answer(text="raise SystemExit('boom')", model_id="stub")
        return Answer(text="print('The answer is 3')", model_id="stub")

    monkeypatch.setattr("jarvis.gateway.client.ask", fake_ask)
    answer = SPEC.handler(upload_id=upload_id, question="what is the total?")
    assert answer["ok"] is True and answer["retried"] is True
    assert attempts["count"] == 2, "one retry, never a loop"


def test_a_script_that_fails_twice_reports_the_real_error(monkeypatch, tmp_path):
    from jarvis.artifacts import office as writer
    from jarvis.gateway.client import Answer
    from jarvis.tools.analyze_spreadsheet import SPEC

    path = tmp_path / "sales.xlsx"
    writer.write_xlsx(path, [["n"], ["1"]])
    upload_id = _attach("sales.xlsx", path.read_bytes())
    monkeypatch.setattr("jarvis.gateway.client.ask",
                        lambda *a, **k: Answer(text="raise ValueError('still broken')",
                                               model_id="stub"))

    answer = SPEC.handler(upload_id=upload_id, question="total?")
    assert answer["ok"] is False
    assert "failed twice" in answer["error"] and "still broken" in answer["error"]


def test_analysing_an_attachment_that_is_gone_says_so():
    from jarvis.tools.analyze_spreadsheet import SPEC

    answer = SPEC.handler(upload_id="nope", question="total?")
    assert answer["ok"] is False and "attach the file again" in answer["error"]
