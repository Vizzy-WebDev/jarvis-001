"""Content analysis: the free glance, and what it refuses to do on its own.

The tests that matter most are about restraint. Sharing something must cost
nothing — no model call, nothing read — because the whole design exists to stop
Jarvis reading a thing first and asking what was wanted afterwards.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis import conversation
from jarvis.background import join_all
from jarvis.content import intake, store
from jarvis.content.investigator import examine, judge_claim, share
from jarvis.db import reset_for_tests as reset_db
from jarvis.documents import office


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    conversation.reset_for_tests()
    yield
    join_all()
    conversation.reset_for_tests()
    reset_db()


@pytest.fixture
def no_network(monkeypatch):
    """Identification must never reach the network in these tests: what is being
    checked is the decision, not the fetch."""
    monkeypatch.setattr(intake, "_oembed", lambda url: None)
    monkeypatch.setattr("jarvis.content.intake.fetch_article",
                        lambda url, **kw: _article(url))


def _article(url):
    from jarvis.webtext import Article

    return Article(ok=True, url=url, title="A real page",
                   text="The kettle boils at one hundred degrees. " * 40)


# --- what did they actually hand over ----------------------------------------

@pytest.mark.parametrize("text,kind", [
    ("https://www.youtube.com/watch?v=abc123", "youtube"),
    ("https://youtu.be/abc123", "youtube"),
    ("https://youtube.com/shorts/abc123", "youtube"),
    ("https://example.com/article", "url"),
    (r"C:\Users\me\notes.txt", "file"),
    ('"C:\\Users\\me\\a file.txt"', "file"),
    ("just some words I typed", "text"),
    ("", "text"),
])
def test_classifying_what_was_shared(text, kind):
    assert intake.classify_source(text)["kind"] == kind


def test_a_sentence_that_starts_with_a_slash_is_not_a_file_path():
    """"/usr/bin is where it lives" is a sentence far more often than a path, so
    a POSIX path only counts when it actually exists."""
    assert intake.classify_source("/usr/bin is where it lives")["kind"] == "text"


def test_a_real_posix_path_is_a_file(tmp_path):
    real = tmp_path / "notes.txt"
    real.write_text("hello")
    assert intake.classify_source(str(real)) == {"kind": "file", "filePath": str(real)}


@pytest.mark.parametrize("url,video_id", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ?t=42", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://example.com/watch?v=nope", None),
])
def test_finding_the_video_id(url, video_id):
    assert intake.youtube_video_id(url) == video_id


# --- sharing costs nothing ----------------------------------------------------

def test_sharing_makes_no_model_call_and_reads_nothing(monkeypatch, no_network):
    """The whole point of the split. A model call here would mean the thing was
    read before anyone said what they wanted from it."""
    def refuse(*_a, **_kw):
        raise AssertionError("sharing must not call a model")

    monkeypatch.setattr("jarvis.gateway.client.ask", refuse)
    result = share(source=intake.classify_source("https://example.com/article"),
                   session_id="s1")
    join_all()

    assert result["record"]["findings"] == []
    assert result["identity"]["title"] == "A real page"
    assert result["record"]["identity"]["kind"] == "article"


def test_a_shared_page_is_fetched_once_and_kept_for_the_reading(no_network):
    result = share(source=intake.classify_source("https://example.com/article"),
                   session_id="s1")
    join_all()
    material = store.get_content(result["record"]["id"])["material"]
    assert material["intake"] == "article"
    assert "kettle" in material["text"]


def test_an_unreachable_page_is_reported_rather_than_guessed_at(monkeypatch):
    from jarvis.webtext import Article

    monkeypatch.setattr("jarvis.content.intake.fetch_article",
                        lambda url, **kw: Article(ok=False, error="Couldn't reach that page."))
    result = share(source={"kind": "url", "url": "https://nope.example"}, session_id="s1")
    join_all()
    assert result["identity"]["unreachable"] == "Couldn't reach that page."


def test_a_missing_file_is_reported_rather_than_registered_as_readable():
    result = share(source={"kind": "file", "filePath": "/no/such/file.mp4"}, session_id="s1")
    join_all()
    assert result["identity"]["unreachable"] == "I can't find that file."


def test_describing_what_something_is():
    assert intake.describe({"kind": "video", "title": "How to bake", "detail": "12 minutes"}) \
        == 'a video — "How to bake" (12 minutes)'
    assert intake.describe({"kind": "text", "title": None, "detail": None}) == "some pasted text"


@pytest.mark.parametrize("kind,phrase", [
    ("video", "watched"),
    ("captions", "did NOT see"),
    ("youtube-text", "neither watched nor heard"),
    ("article", "read the article"),
])
def test_how_it_was_taken_in_is_always_said_honestly(kind, phrase):
    assert phrase in intake.intake_description(kind)


# --- examining ----------------------------------------------------------------

def _answer(text="Because it is hot.", data=None, model_id="stub"):
    from jarvis.gateway.client import Answer

    return Answer(text=text, model_id=model_id, data=data)


def test_examining_text_answers_the_question_asked_and_records_it(monkeypatch, no_network):
    monkeypatch.setattr("jarvis.gateway.client.ask",
                        lambda *a, **k: _answer("It boils at one hundred degrees."))
    shared = share(source=intake.classify_source("https://example.com/article"),
                   session_id="s1")
    join_all()

    examine(shared["record"]["id"], "at what temperature does it boil?", session_id="s1")
    join_all()

    record = store.get_content(shared["record"]["id"])
    assert len(record["findings"]) == 1
    assert record["findings"][0]["request"] == "at what temperature does it boil?"
    assert record["findings"][0]["intake"] == "article"

    # ...and it lands in the conversation, not only in the record.
    said = "\n".join(m.get("text") or "" for m in conversation.get_messages("s1"))
    assert "one hundred degrees" in said and "How I took it in" in said


def test_a_failed_examine_is_recorded_in_the_conversation_too(monkeypatch, no_network):
    """A failure that only reached the event stream left the model with no record
    anything was asked — so "what did that turn up?" got nothing at all, on a
    roster where running out of quota mid-job is the normal case."""
    def unavailable(*_a, **_kw):
        raise RuntimeError("everything is rate limited")

    monkeypatch.setattr("jarvis.gateway.client.ask", unavailable)
    shared = share(source=intake.classify_source("https://example.com/article"),
                   session_id="s1")
    join_all()
    examine(shared["record"]["id"], "what does it say?", session_id="s1")
    join_all()

    said = "\n".join(m.get("text") or "" for m in conversation.get_messages("s1"))
    assert "didn't work" in said and "rate limited" in said
    assert store.get_content(shared["record"]["id"])["findings"] == []


def test_a_follow_up_the_notes_cover_does_not_look_again(monkeypatch):
    calls = {"cached": 0, "fresh": 0}

    def fake_ask(prompt, **kw):
        if "Working notes" in prompt:
            calls["cached"] += 1
            return _answer(data={"answer": "Blue.", "needsAnotherLook": False})
        calls["fresh"] += 1
        return _answer(data={"answer": "fresh", "observations": "notes"})

    monkeypatch.setattr("jarvis.gateway.client.ask", fake_ask)
    record = store.create_content(source={"kind": "file", "filePath": "/x/clip.mp4"},
                                  identity={"title": "clip", "kind": "video"},
                                  session_id="s1")
    store.cache_material(record["id"], {"observations": "a blue car drives past",
                                        "intake": "video"})

    examine(record["id"], "what colour was the car?", session_id="s1")
    join_all()
    assert calls == {"cached": 1, "fresh": 0}
    assert store.get_content(record["id"])["findings"][0]["answer"] == "Blue."


def test_notes_that_do_not_cover_the_question_cause_a_real_second_look(monkeypatch):
    looked = {"again": False}

    def fake_ask(prompt, **kw):
        if "Working notes" in prompt:
            return _answer(data={"answer": "", "needsAnotherLook": True,
                                 "why": "the notes say nothing about sound"})
        looked["again"] = True
        return _answer(data={"answer": "A siren.", "observations": "a siren sounds"})

    monkeypatch.setattr("jarvis.gateway.client.ask", fake_ask)
    monkeypatch.setattr("jarvis.gateway.routing.build_candidates",
                        lambda task, **kw: [{"id": "m1", "adapter": "gemini",
                                             "model": "g", "caps": {"video": True}}])
    # Patched where the investigator BOUND it, not where it is defined: the
    # module-level import means the other target would have no effect at all.
    monkeypatch.setattr("jarvis.content.investigator.prepare_for",
                        lambda entry, source: {"media": [{"kind": "video"}], "cleanup": None})

    record = store.create_content(source={"kind": "file", "filePath": "/x/clip.mp4"},
                                  identity={"title": "clip", "kind": "video"},
                                  session_id="s1")
    store.cache_material(record["id"], {"observations": "a blue car", "intake": "video"})
    examine(record["id"], "what could you hear?", session_id="s1")
    join_all()

    assert looked["again"] is True
    assert store.get_content(record["id"])["findings"][0]["answer"] == "A siren."


def test_examining_an_office_document_reads_its_real_content(monkeypatch, tmp_path):
    from jarvis.artifacts import office as writer

    path = tmp_path / "notes.docx"
    writer.write_docx(path, ["The deposit is due on the third."])
    seen = {}

    def fake_ask(prompt, **kw):
        seen["prompt"] = prompt
        return _answer("The third.")

    monkeypatch.setattr("jarvis.gateway.client.ask", fake_ask)
    shared = share(source={"kind": "file", "filePath": str(path)}, session_id="s1")
    join_all()
    examine(shared["record"]["id"], "when is the deposit due?", session_id="s1")
    join_all()

    assert "deposit is due on the third" in seen["prompt"]


def test_content_is_resolved_within_one_conversation_not_across_all_of_them():
    store.create_content(source={"kind": "text", "text": "a"}, identity={"title": "a"},
                         session_id="other")
    mine = store.create_content(source={"kind": "text", "text": "b"}, identity={"title": "b"},
                                session_id="s1")
    assert store.latest_in_session("s1")["id"] == mine["id"]
    assert store.latest_in_session("nobody") is None


# --- checking a claim ---------------------------------------------------------

def test_a_claim_is_always_researched_before_it_is_judged(monkeypatch):
    """The one guarantee carried over unchanged: research runs at the top, with
    no path around it, including for a model that thinks it already knows."""
    from jarvis.research import Research

    researched = {"count": 0}

    def fake_research(question, search_query=None):
        researched["count"] += 1
        return Research(ok=True, answer="Most people make nothing.", query="x")

    monkeypatch.setattr("jarvis.content.investigator.research", fake_research)
    monkeypatch.setattr("jarvis.gateway.client.ask",
                        lambda *a, **k: _answer(data={"verdict": "misleading",
                                                      "confidence": "high",
                                                      "reasoning": "It is not typical."}))
    judged = judge_claim("you can make $12,000 in your first month")
    assert researched["count"] == 1
    assert judged["verdict"] == "misleading"
    assert "misleading" in judged["result"] and "not typical" in judged["result"]


def test_an_unrecognised_verdict_becomes_cannot_tell_rather_than_being_passed_through(monkeypatch):
    from jarvis.research import Research

    monkeypatch.setattr("jarvis.content.investigator.research",
                        lambda *a, **k: Research(ok=True, answer="something"))
    monkeypatch.setattr("jarvis.gateway.client.ask",
                        lambda *a, **k: _answer(data={"verdict": "definitely yes!!"}))
    assert judge_claim("something")["verdict"] == "can't tell"


def test_a_claim_that_could_not_be_researched_says_so_in_the_verdict(monkeypatch):
    from jarvis.research import Research

    monkeypatch.setattr("jarvis.content.investigator.research",
                        lambda *a, **k: Research(ok=False, error="no search available"))
    monkeypatch.setattr("jarvis.gateway.client.ask",
                        lambda *a, **k: _answer(data={"verdict": "can't tell",
                                                      "reasoning": "nothing to go on"}))
    judged = judge_claim("something")
    assert judged["researched"] is False
    assert "couldn't look this up independently" in judged["result"]


def test_an_empty_claim_is_refused():
    assert judge_claim("   ")["ok"] is False


# --- the tools ----------------------------------------------------------------

def test_share_content_asks_what_is_wanted_when_they_did_not_say(monkeypatch, no_network):
    from jarvis.tools.content_tools import SPECS

    monkeypatch.setattr("jarvis.gateway.client.ask",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no model call")))
    share_tool = next(s for s in SPECS if s.name == "share_content")
    answer = share_tool.handler(source="https://example.com/article")
    join_all()
    assert answer["ok"] is True
    assert "ask what they want" in answer["spoken_hint"]
    assert "Nothing has been read" in answer["note"]


def test_share_content_with_an_instruction_looks_into_it_straight_away(monkeypatch, no_network):
    from jarvis.tools.content_tools import SPECS

    monkeypatch.setattr("jarvis.gateway.client.ask", lambda *a, **k: _answer("Water."))
    share_tool = next(s for s in SPECS if s.name == "share_content")
    answer = share_tool.handler(source="https://example.com/article",
                                instruction="what is it about?")
    join_all()
    assert "looking into it" in answer["spoken_hint"]
    assert len(store.get_content(answer["contentId"])["findings"]) == 1


def test_examine_content_with_nothing_shared_says_so_rather_than_failing_oddly():
    from jarvis.tools.content_tools import SPECS

    examine_tool = next(s for s in SPECS if s.name == "examine_content")
    answer = examine_tool.handler(request="what does it say?")
    assert answer["ok"] is False and "nothing I've been shown" in answer["error"]


def test_the_office_reader_reads_a_real_generated_document(tmp_path):
    from jarvis.artifacts import office as writer

    path = tmp_path / "report.docx"
    writer.write_docx(path, ["A heading", "Some real body text."])
    read = office.extract_document(path)
    assert read["ok"] is True and "Some real body text." in read["markdown"]


def test_a_workbook_reports_that_it_was_truncated_rather_than_looking_complete(tmp_path):
    from jarvis.artifacts import office as writer

    path = tmp_path / "big.xlsx"
    writer.write_xlsx(path, [["n"], *[[str(i)] for i in range(50)]])
    read = office.extract_document(path, max_rows_per_sheet=10)
    assert read["truncated"] is True
    assert "more rows not shown" in read["markdown"]
    assert "analyse the whole file" in read["note"]
