"""The Specialists screen's HTTP surface, and talking to a specialist directly.

Through FastAPI's real test client and the real app. The claims: built-in and
custom agents come back the same way, a change takes effect on the NEXT turn (the
live `ask_specialist` declaration is re-synced, no restart), the ability picker
keeps built-ins and Skills apart, and a direct chat with a specialist is a real
turn run as that agent on the person's own conversation.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from jarvis import assembly, conversation
from jarvis.agents import store
from jarvis.db import reset_for_tests as reset_db
from jarvis.events import bus
from jarvis.main import create_app
from jarvis.session import get_active_session_id

from session_scripted_model import SessionScriptedModel, install


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    conversation.reset_for_tests()
    assembly.reset_for_tests()
    bus.reset_for_tests()
    yield
    assembly.reset_for_tests()
    conversation.reset_for_tests()
    reset_db()


@pytest.fixture
def client():
    return TestClient(create_app())


def events_from(response) -> list[dict]:
    return [json.loads(line[6:]) for line in response.text.splitlines()
            if line.startswith("data: ")]


def declared_roster() -> list[str]:
    spec = assembly.get_registry().get("ask_specialist")
    return spec.input_schema["properties"]["agent"]["enum"] if spec else []


def test_the_roster_lists_every_builtin_with_its_last_run(client):
    agents = client.get("/api/agents").json()["agents"]
    assert len(agents) == 13 and all(a["builtin"] for a in agents)
    assert all("lastRun" in a for a in agents)


def test_create_edit_disable_and_delete_a_custom_agent_takes_effect_at_once(client):
    made = client.post("/api/agents", json={
        "name": "Podcast Producer", "description": "Plans and scripts podcast episodes",
        "mission": "Make great episodes", "doctrine": "Outline, script, show notes.",
        "collaborators": ["research", "content"],
        "capabilityAccess": {"mode": "selected", "names": ["look_it_up", "create_artifact"]},
        "memoryAccess": "none"}).json()
    assert made["ok"] and made["agent"]["id"] == "podcast-producer"
    assert "podcast-producer" in declared_roster()   # offered on the very next turn

    edited = client.patch("/api/agents/podcast-producer",
                          json={"guardrails": "No explicit content.", "enabled": False}).json()
    assert edited["agent"]["guardrails"] == "No explicit content."
    assert "podcast-producer" not in declared_roster()

    assert client.delete("/api/agents/podcast-producer").json() == {"ok": True}
    assert client.get("/api/agents/podcast-producer").status_code == 404


def test_a_builtin_is_reset_not_deleted(client):
    client.patch("/api/agents/teacher", json={"doctrine": "mine"})
    refused = client.delete("/api/agents/teacher")
    assert refused.status_code == 400 and "can be turned off or reset" in refused.json()["error"]
    reset = client.post("/api/agents/teacher/reset").json()
    assert reset["agent"]["doctrine"] != "mine"
    assert client.post("/api/agents/podcast/reset").status_code == 400


def test_a_nameless_agent_is_refused_in_words(client):
    response = client.post("/api/agents", json={"name": "  "})
    assert response.status_code == 400 and response.json()["error"] == "An agent needs a name."


def test_the_ability_picker_keeps_builtins_skills_and_connectors_apart(client):
    abilities = client.get("/api/agents/abilities").json()
    names = {a["name"] for a in abilities["builtIn"]}
    assert "look_it_up" in names and "narrate_to_file" in names
    # An agent's own tools are not offered: it always has them.
    assert not names & {"ask_specialist", "read_my_notes", "write_my_note", "find_capability"}
    assert abilities["skills"] == []           # only folder Skills, and none are installed
    assert {c["type"] for c in abilities["connectors"]} >= {"files", "browser"}


def test_a_direct_chat_is_a_real_turn_as_that_specialist(client):
    model = install(assembly, SessionScriptedModel())
    session = get_active_session_id()
    model.speaker_for_session[session] = "teacher"
    model.on("teacher").calls_tool("write_my_note", {"topic": "learner: spanish",
                                                     "text": "Beginner; goal: order food"})
    model.on("teacher").says("¡Hola! Let's start: how would you say 'a coffee, please'?")

    events = events_from(client.get("/api/chat/stream", params={
        "message": "Teach me enough Spanish to order food", "agent": "teacher"}))
    done = [e for e in events if e["type"] == "done"][0]
    assert done["agent"] == {"id": "teacher", "name": "Teacher"}
    system = model.requests_of("teacher")[0]["system"]
    assert system.startswith("You are Teacher") and "talking to you directly" in system

    [run] = store.list_runs()
    assert run["requestedBy"] == "operator" and run["sessionId"] == session
    assert run["status"] == "done" and run["toolsUsed"] == ["write_my_note"]
    # The note landed with the Teacher, and the exchange is in the person's own chat.
    assert store.list_notes("teacher")[0]["text"] == "Beginner; goal: order food"
    texts = [m.get("text") for m in conversation.get_messages(session)]
    assert "Teach me enough Spanish to order food" in texts

    # The run shows up on the agent's own page, with its tree.
    detail = client.get("/api/agents/teacher").json()
    assert detail["runs"][0]["id"] == run["id"] and detail["notes"][0]["topic"] == "learner: spanish"
    tree = client.get(f"/api/agent-runs/{run['id']}").json()
    assert tree["run"]["agentName"] == "Teacher" and len(tree["tree"]) == 1


def test_a_direct_chat_with_a_switched_off_specialist_is_refused(client):
    client.patch("/api/agents/scout", json={"enabled": False})
    response = client.get("/api/chat/stream", params={"message": "hi", "agent": "scout"})
    assert response.status_code == 400 and "turned off" in response.json()["error"]
