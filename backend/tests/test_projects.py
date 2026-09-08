"""The planning partner: five steps, and nothing chains into the next.

The restraint is the feature. An earlier design researched an idea the moment it
was mentioned and wrote the plan the instant its question queue emptied; several
of these tests exist purely to prove that cannot happen here.
"""

from __future__ import annotations

import pytest

from jarvis import conversation
from jarvis.background import join_all
from jarvis.db import reset_for_tests as reset_db
from jarvis.projects import assistants, store
from jarvis.projects.engine import (
    note_decision, research_project, start_project, write_plan, write_prompts,
)


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    conversation.reset_for_tests()
    yield
    join_all()
    conversation.reset_for_tests()
    reset_db()


def _answer(text="", data=None, model_id="stub"):
    from jarvis.gateway.client import Answer

    return Answer(text=text, model_id=model_id, data=data)


# --- starting -----------------------------------------------------------------

def test_starting_a_project_makes_no_model_call_and_researches_nothing(monkeypatch):
    """A notepad opening, not a process starting."""
    def refuse(*_a, **_kw):
        raise AssertionError("starting a project must not call a model")

    monkeypatch.setattr("jarvis.gateway.client.ask", refuse)
    monkeypatch.setattr("jarvis.projects.engine.research", refuse)

    project = start_project(idea="a website for my bakery", session_id="s1")
    join_all()
    assert project["research"] is None and project["plan"] is None
    assert project["title"] == "a website for my bakery"


def test_an_idea_is_kept_in_their_own_words():
    long_idea = ("an app where people in my street can lend each other tools, "
                 "with a map and a deposit system")
    project = start_project(idea=long_idea, session_id="s1")
    assert project["idea"] == long_idea
    assert len(project["title"].split()) <= 8, "the title is short; the idea is not summarised"


def test_a_project_needs_an_idea():
    with pytest.raises(ValueError):
        start_project(idea="   ", session_id="s1")


# --- decisions ----------------------------------------------------------------

def test_decisions_accumulate_rather_than_replacing_each_other():
    project = start_project(idea="a bakery site", session_id="s1")
    note_decision(project["id"], "no online payments in the first version")
    note_decision(project["id"], "one page, not a whole site")
    decisions = store.get_project(project["id"])["decisions"]
    assert [d["text"] for d in decisions] == ["no online payments in the first version",
                                              "one page, not a whole site"]
    assert all(d["at"] for d in decisions)


def test_a_decision_on_a_project_that_is_gone_is_refused_plainly():
    with pytest.raises(KeyError):
        note_decision("pjnope", "something")


def test_an_empty_decision_changes_nothing():
    project = start_project(idea="a bakery site", session_id="s1")
    note_decision(project["id"], "   ")
    assert store.get_project(project["id"])["decisions"] == []


# --- research -----------------------------------------------------------------

def test_research_only_runs_when_asked_and_lands_in_the_conversation(monkeypatch):
    from jarvis.research import Research

    monkeypatch.setattr("jarvis.projects.engine.research",
                        lambda q, search_query=None: Research(
                            ok=True, answer="A one-page site costs about £0.",
                            query=q, model_id="m1"))

    project = start_project(idea="a bakery site", session_id="s1")
    research_project(project["id"])
    join_all()

    saved = store.get_project(project["id"])
    assert saved["research"]["answer"].startswith("A one-page site")
    assert [h["step"] for h in saved["history"]] == ["research"]
    said = "\n".join(m.get("text") or "" for m in conversation.get_messages("s1"))
    assert "the research is ready" in said and "about £0" in said


def test_research_that_fails_is_recorded_and_said_rather_than_left_silent(monkeypatch):
    from jarvis.research import Research

    monkeypatch.setattr("jarvis.projects.engine.research",
                        lambda q, search_query=None: Research(ok=False, error="nothing found"))
    project = start_project(idea="a bakery site", session_id="s1")
    research_project(project["id"])
    join_all()

    assert store.get_project(project["id"])["research"]["error"] == "nothing found"
    said = "\n".join(m.get("text") or "" for m in conversation.get_messages("s1"))
    assert "didn't work" in said and "nothing found" in said


# --- the plan -----------------------------------------------------------------

def test_the_plan_is_written_from_the_decisions_not_only_the_idea(monkeypatch):
    seen = {}

    def fake_ask(prompt, **kw):
        seen["prompt"] = prompt
        return _answer("## What you're building\n\nA one-page site.")

    monkeypatch.setattr("jarvis.gateway.client.ask", fake_ask)
    conversation.push_user_text("s1", "I want it to feel warm and handmade")

    project = start_project(idea="a bakery site", session_id="s1")
    note_decision(project["id"], "no online payments in the first version")
    write_plan(project["id"])
    join_all()

    assert "no online payments" in seen["prompt"]
    assert "treat these as fixed" in seen["prompt"]
    assert "warm and handmade" in seen["prompt"], "the conversation is context too"
    assert store.get_project(project["id"])["plan"].startswith("## What you're building")


def test_research_is_not_required_before_a_plan(monkeypatch):
    monkeypatch.setattr("jarvis.gateway.client.ask", lambda *a, **k: _answer("A plan."))
    project = start_project(idea="a bakery site", session_id="s1")
    write_plan(project["id"])
    join_all()
    assert store.get_project(project["id"])["plan"] == "A plan."


def test_a_plan_that_cannot_be_written_says_so_instead_of_leaving_an_empty_project(monkeypatch):
    def unavailable(*_a, **_kw):
        raise RuntimeError("everything is rate limited")

    monkeypatch.setattr("jarvis.gateway.client.ask", unavailable)
    project = start_project(idea="a bakery site", session_id="s1")
    write_plan(project["id"])
    join_all()

    saved = store.get_project(project["id"])
    assert saved["plan"] is None and "rate limited" in saved["error"]
    said = "\n".join(m.get("text") or "" for m in conversation.get_messages("s1"))
    assert "didn't work" in said


# --- the handoff prompts ------------------------------------------------------

def test_prompts_need_a_plan_first():
    project = start_project(idea="a bakery site", session_id="s1")
    with pytest.raises(ValueError):
        write_prompts(project["id"], {"id": "chat"})


def test_the_prompt_is_written_for_the_named_assistant(monkeypatch):
    seen = {}

    def fake_ask(prompt, **kw):
        seen["prompt"] = prompt
        return _answer(data={"prompts": [{"title": "Build the site", "text": "Do the thing."}]})

    monkeypatch.setattr("jarvis.gateway.client.ask", fake_ask)
    project = start_project(idea="a bakery site", session_id="s1")
    store.update_project(project["id"], {"plan": "## The plan\n\nOne page."})
    write_prompts(project["id"], {"id": "builder"})
    join_all()

    assert "app-building tool" in seen["prompt"], "the target's own guidance is used"
    saved = store.get_project(project["id"])
    assert saved["prompts"] == [{"n": 1, "title": "Build the site", "text": "Do the thing."}]
    assert saved["promptOrder"] is None, "one prompt needs no ordering note"


def test_several_prompts_keep_their_order_and_the_reason_for_it(monkeypatch):
    monkeypatch.setattr("jarvis.gateway.client.ask", lambda *a, **k: _answer(data={
        "prompts": [{"title": "One", "text": "first"}, {"title": "Two", "text": "second"}],
        "order": "The second needs the database from the first."}))
    project = start_project(idea="a bakery site", session_id="s1")
    store.update_project(project["id"], {"plan": "A plan"})
    write_prompts(project["id"], {"id": "claude-code"})
    join_all()

    saved = store.get_project(project["id"])
    assert [p["n"] for p in saved["prompts"]] == [1, 2]
    assert "database from the first" in saved["promptOrder"]


def test_a_reply_with_no_usable_prompt_says_so_and_keeps_the_plan(monkeypatch):
    monkeypatch.setattr("jarvis.gateway.client.ask",
                        lambda *a, **k: _answer(data={"prompts": [{"title": "x", "text": "  "}]}))
    project = start_project(idea="a bakery site", session_id="s1")
    store.update_project(project["id"], {"plan": "A plan"})
    write_prompts(project["id"], {"id": "chat"})
    join_all()

    saved = store.get_project(project["id"])
    assert saved["prompts"] is None
    assert "still ready on its own" in saved["error"]
    assert saved["plan"] == "A plan"


def test_a_custom_assistant_is_named_in_its_own_guidance():
    guidance = assistants.guidance_for({"id": "custom", "name": "Aider"})
    assert "Aider" in guidance
    assert assistants.describe_assistant({"id": "custom", "name": "Aider"}) == "Aider"
    assert assistants.describe_assistant(None) == "an AI assistant"


# --- scoping ------------------------------------------------------------------

def test_a_follow_up_resolves_within_one_conversation_only():
    start_project(idea="someone else's idea", session_id="other")
    mine = start_project(idea="my idea", session_id="s1")
    assert store.latest_in_session("s1")["id"] == mine["id"]
    assert store.latest_in_session("nobody") is None


# --- the tools ----------------------------------------------------------------

def _tool(name):
    from jarvis.tools.project_tools import SPECS

    return next(s for s in SPECS if s.name == name)


def test_the_tools_resolve_the_project_being_discussed(monkeypatch):
    monkeypatch.setattr("jarvis.session.get_active_session_id", lambda: "s1")
    started = _tool("start_project").handler(idea="a bakery site")
    assert started["ok"] is True

    noted = _tool("note_project_decision").handler(decision="one page only")
    assert noted["projectId"] == started["projectId"]
    assert store.get_project(started["projectId"])["decisions"][0]["text"] == "one page only"


def test_a_tool_with_no_project_in_play_says_so(monkeypatch):
    monkeypatch.setattr("jarvis.session.get_active_session_id", lambda: "s1")
    answer = _tool("write_project_plan").handler()
    assert answer["ok"] is False and "no project on the go" in answer["error"]


def test_writing_prompts_asks_which_assistant_rather_than_choosing_one(monkeypatch):
    monkeypatch.setattr("jarvis.session.get_active_session_id", lambda: "s1")
    started = _tool("start_project").handler(idea="a bakery site")
    store.update_project(started["projectId"], {"plan": "A plan"})

    answer = _tool("write_build_prompts").handler()
    assert answer["ok"] is False
    assert "which AI is going to build this" in answer["error"]


def test_writing_prompts_before_a_plan_is_refused_with_the_reason(monkeypatch):
    monkeypatch.setattr("jarvis.session.get_active_session_id", lambda: "s1")
    _tool("start_project").handler(idea="a bakery site")
    answer = _tool("write_build_prompts").handler(target="chat")
    assert answer["ok"] is False and "written plan yet" in answer["error"]
