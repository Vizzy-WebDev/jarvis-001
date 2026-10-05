"""Specialist agents: definitions, seeding, runs and notes, as rows.

The claim these pin down is structural: a built-in agent and a custom one are the
same kind of row, the person's edits to a built-in survive an upgrade of its
default text, and only the things that differ (delete versus reset) differ.
"""

from __future__ import annotations

import pytest

from jarvis.agents import ensure_builtins, store
from jarvis.agents.builtins import BUILTIN_AGENTS, builtin
from jarvis.db import reset_for_tests as reset_db


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    yield
    reset_db()


def test_all_thirteen_builtins_are_seeded_once_and_are_ordinary_rows():
    assert len(ensure_builtins()) == 13
    assert ensure_builtins() == []  # idempotent
    agents = store.list_agents()
    assert {a["id"] for a in agents} == {d["id"] for d in BUILTIN_AGENTS}
    assert all(a["builtin"] and a["enabled"] for a in agents)
    # Voice and audio belong to Video Production — there is no separate agent for them.
    assert not any("voice" in a["id"] or "audio" in a["id"] for a in agents)


def test_every_builtin_names_only_collaborators_that_exist():
    ids = {d["id"] for d in BUILTIN_AGENTS}
    for definition in BUILTIN_AGENTS:
        collaborators = definition["collaborators"]
        if collaborators != "any":
            assert set(collaborators) <= ids, definition["id"]
            assert definition["id"] not in collaborators


def test_a_custom_agent_is_the_same_shape_as_a_builtin():
    ensure_builtins()
    made = store.create_agent({"name": "Podcast Producer", "description": "Plans podcast episodes",
                               "mission": "m", "doctrine": "d", "collaborators": ["research"],
                               "capabilityAccess": {"mode": "selected", "names": ["look_it_up"]}})
    assert made["id"] == "podcast-producer" and made["builtin"] is False
    assert set(made) == set(store.get_agent("research"))
    assert made["capabilityAccess"] == {"mode": "selected", "names": ["look_it_up"],
                                        "connectors": "all"}
    # A second one with the same name gets its own id rather than overwriting.
    assert store.create_agent({"name": "Podcast Producer"})["id"] == "podcast-producer-2"


def test_a_builtin_can_be_edited_disabled_and_reset_but_not_deleted():
    ensure_builtins()
    store.update_agent("teacher", {"doctrine": "my own way", "enabled": False, "builtin": False})
    edited = store.get_agent("teacher")
    assert edited["doctrine"] == "my own way" and edited["enabled"] is False
    assert edited["builtin"] is True  # not an editable field
    with pytest.raises(store.AgentError):
        store.delete_agent("teacher")
    reset = store.reset_builtin("teacher", builtin("teacher"))
    assert reset["doctrine"] == builtin("teacher")["doctrine"] and reset["enabled"] is True


def test_a_newer_default_refreshes_an_untouched_builtin_but_never_an_edited_one():
    ensure_builtins()
    store.update_agent("scout", {"mission": "mine"})
    newer = [{**d, "version": 99, "mission": "new default"} for d in BUILTIN_AGENTS
             if d["id"] in ("scout", "research")]
    assert store.seed_builtins(newer) == ["research"]
    assert store.get_agent("research")["mission"] == "new default"
    assert store.get_agent("scout")["mission"] == "mine"


def test_a_custom_agent_can_be_deleted_with_its_notes():
    made = store.create_agent({"name": "Temp"})
    store.write_note(made["id"], "plan", "x")
    store.delete_agent(made["id"])
    assert store.get_agent(made["id"]) is None and store.list_notes(made["id"]) == []


def test_find_agent_by_name_however_it_was_typed():
    ensure_builtins()
    assert store.find_agent("research")["id"] == "research"
    assert store.find_agent("Strategy & Business")["id"] == "strategy"
    assert store.find_agent("video production")["id"] == "video-production"
    assert store.find_agent("nobody") is None


def test_runs_form_a_tree_under_one_root():
    root = store.create_run(agent_id="advertising", task="plan", session_id="agent:advertising:c1",
                            requested_by="jarvis", conversation_id="c1")
    child = store.create_run(agent_id="research", task="look", session_id="agent:research:c1",
                             requested_by="advertising", parent_run_id=root["id"],
                             root_run_id=root["id"], depth=2)
    assert root["rootRunId"] == root["id"] and child["rootRunId"] == root["id"]
    assert store.count_runs_under(root["id"]) == 2
    assert store.running_run_for_session("agent:research:c1")["id"] == child["id"]
    store.finish_run(child["id"], status="done", result="found", tools_used=["look_it_up"])
    assert store.running_run_for_session("agent:research:c1") is None
    assert store.get_run(child["id"])["toolsUsed"] == ["look_it_up"]


def test_notes_replace_or_append_under_a_topic():
    store.write_note("teacher", "learner: python", "level: beginner")
    store.write_note("teacher", "Learner: Python", "level: loops mastered")
    assert [n["text"] for n in store.list_notes("teacher")] == ["level: loops mastered"]
    store.write_note("scout", "surfaced", "grant A", mode="append")
    store.write_note("scout", "surfaced", "grant B", mode="append")
    assert store.list_notes("scout", "surfaced")[0]["text"] == "grant A\ngrant B"
    with pytest.raises(store.AgentError):
        store.write_note("scout", "", "x")
