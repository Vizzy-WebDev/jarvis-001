"""Making an artifact: asked for means made, in the chat that asked, in any format.

The rule this pins down (confirmed with the person who owns this app): when they
ASK for a file it is made at once, with no confirmation step; when they did not
ask, Jarvis offers first. The first half is structural (LOW risk, always
declared); the second is the prompt's instruction, checked here for presence.
"""

from __future__ import annotations

import pytest

from jarvis import artifacts, assembly, chat_store, conversation, prompt
from jarvis.artifacts.pdf import write_pdf
from jarvis.capabilities import Risk
from jarvis.db import reset_for_tests as reset_db
from jarvis.documents import read_pdf
from jarvis.orchestrator.pipeline import TurnRequest
from jarvis.policy import Autonomy, CallContext, Surface
from jarvis.tools.create_artifact import _run

from session_scripted_model import SessionScriptedModel, install


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    conversation.reset_for_tests()
    assembly.reset_for_tests()
    yield
    assembly.reset_for_tests()
    conversation.reset_for_tests()
    reset_db()


def _ctx(session: str) -> CallContext:
    return CallContext(session_id=session, turn_id="t1", surface=Surface.TEXT,
                       autonomy=Autonomy.INTERACTIVE)


# --- the rule --------------------------------------------------------------------------------------

def test_making_a_file_is_low_risk_and_offered_on_every_turn():
    spec = assembly.get_registry().get("create_artifact")
    assert spec.risk is Risk.LOW and spec.has_tag("core") and spec.wants_context


def test_an_explicit_request_makes_the_file_in_that_turn_with_no_question():
    chat = chat_store.create_conversation()["id"]
    conversation.bind_session(chat)
    model = install(assembly, SessionScriptedModel())
    model.on("jarvis").calls_tool("create_artifact", {"filename": "plan.md", "content": "# Plan"})
    model.on("jarvis").says("Your plan is ready.")
    events = list(assembly.get_orchestrator().run_turn(TurnRequest(
        text="make me a plan as a file", session_id=chat, surface=Surface.TEXT,
        autonomy=Autonomy.INTERACTIVE)))
    assert not [e for e in events if type(e).__name__ == "ApprovalRequired"]
    [made] = artifacts.list_page()[0]
    assert made.name == "plan.md" and made.conversation_id == chat


def test_the_prompt_says_to_make_when_asked_and_to_offer_when_not():
    text = prompt.stable_instruction()
    assert "make it with create_artifact straight away" in text
    assert "offer it in one short sentence" in text and "only after they say yes" in text


# --- formats ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["app.py", "styles.css", "query.sql", "data.yaml", "notes.md",
                                  "page.html", "diagram.svg", "data.json", "readme.txt"])
def test_any_text_format_is_written_exactly_as_given(name):
    result = _run(filename=name, content="exact content\n", ctx=_ctx("s"))
    assert result["ok"], result
    assert artifacts.get(result["id"]).path.read_text() == "exact content\n"


@pytest.mark.parametrize("name,words", [
    ("setup.bat", "runs it on the computer"), ("tool.exe", "runs it on the computer"),
    ("script.ps1", "runs it on the computer"), ("photo.png", "can't make images"),
    ("song.mp3", "narrate_to_file"), ("old.doc", "make a .docx instead"),
])
def test_refused_formats_say_why_and_leave_nothing_behind(name, words):
    result = _run(filename=name, content="x", ctx=_ctx("s"))
    assert result["ok"] is False and words in result["error"]
    assert artifacts.list_page()[0] == []


def test_an_empty_file_is_not_made():
    assert _run(filename="empty.md", content="  ", ctx=_ctx("s"))["ok"] is False


def test_a_pdf_round_trips_through_an_independent_reader(tmp_path):
    text = ("# Report\n\nSales rose.\n\n- first point\n- second point\n\n"
            + "A sentence that wraps over the line many times. " * 200)
    path, lost = write_pdf(tmp_path / "r.pdf", text, title="Report")
    pages = read_pdf(path)
    assert lost == 0 and len(pages) >= 2
    joined = "\n".join(pages)
    assert "Report" in joined and "Sales rose." in joined and "second point" in joined


def test_a_pdf_says_how_many_characters_its_fonts_could_not_show():
    result = _run(filename="greek.pdf", content="Hello λόγος", ctx=_ctx("s"))
    assert result["ok"] and result["verified"] is True
    assert "5 characters couldn't be shown" in result["warning"]


def test_a_broken_pdf_is_not_verified(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4\nnot really\n%%EOF\n")
    assert artifacts.verify(bad)[0] is False


def test_the_card_names_the_artifact_so_the_chat_can_open_it():
    result = _run(filename="a.md", content="# A", title="Alpha", ctx=_ctx("s"))
    action = result["ui_action"]
    assert action["artifactId"] == result["id"] and action["title"] == "Alpha"
    assert action["artifactKind"] == "markdown"


# --- after the person allows something --------------------------------------------------------------

def test_an_allowed_call_replaces_its_parked_result_in_the_saved_conversation():
    chat = chat_store.create_conversation()["id"]
    conversation.bind_session(chat)
    conversation.push_user_text(chat, "run it")
    conversation.push_assistant_tool_calls(chat, [{"id": "call_1", "name": "run_code", "args": {}}])
    conversation.push_tool_results(chat, [{"id": "call_1", "name": "run_code",
                                           "result": {"error": "run_code needs confirming"}}])
    assert conversation.settle_tool_result(chat, "call_1", "run_code", {"ok": True, "stdout": "2"})
    in_memory = conversation.get_messages(chat)[-1]["toolResults"][0]["result"]
    saved = chat_store.get_messages(chat)[-1]["toolResults"][0]["result"]
    assert in_memory == saved == {"ok": True, "stdout": "2"}
