"""The morning briefing's settings, and the things being watched for.

Both stores are tested on their own. What matters here is the briefing's two
compose paths — the cheap narration-only one, and the tool-using one that exists
because connectors were chosen — and that stopping a monitor tells every open
tab rather than only the one whose button was clicked.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from jarvis.events import EventType, bus
from jarvis.gateway import connections, registry
from jarvis.monitor import store as monitor_store
from jarvis.scheduler import briefing, briefing_config

from stub_openai_server import StubModelServer


@pytest.fixture
def client(scratch):
    from jarvis.main import create_app

    return TestClient(create_app())


@pytest.fixture
def stub(scratch):
    server = StubModelServer()
    server.base_url = server.start()
    conn = connections.add_connection(adapter="openai-compatible", base_url=server.base_url,
                                      label="stub", provider="custom", kind="local",
                                      key_required=False)
    registry.add_model(connection_id=conn["id"], model="stub-model")
    yield server
    server.stop()


WATCH_FOR_A_FILE = {"description": "the report landing in Downloads",
                    "check": {"type": "file_exists", "path": "C:/Downloads/report.pdf"},
                    "on_trigger": {"type": "notify", "text": "it landed"}}


# --- the briefing ----------------------------------------------------------------

def test_an_empty_install_answers_the_recorded_shape(client):
    assert client.get("/api/briefing").json() == {
        "sections": {"greeting": True, "dateTime": True, "tasks": True, "goals": True,
                     "focus": True, "custom": False},
        "customText": "", "weatherPlace": "", "headlines": False, "connectors": [],
    }


def test_saving_merges_rather_than_replaces(client):
    """The store owns which keys exist, so a screen that knows about five
    sections cannot silently drop a sixth it has never heard of."""
    saved = client.post("/api/briefing", json={"sections": {"custom": True},
                                               "customText": "mention the gym"}).json()
    assert saved["sections"]["custom"] is True
    assert saved["sections"]["greeting"] is True  # untouched, not dropped
    assert saved["customText"] == "mention the gym"
    assert client.get("/api/briefing").json()["customText"] == "mention the gym"


def test_a_preview_composes_one_for_real(client, stub):
    stub.says("Good morning. Nothing is scheduled today.")
    preview = client.post("/api/briefing/preview").json()
    assert preview["ok"] is True
    assert preview["text"] == "Good morning. Nothing is scheduled today."
    assert preview["modelId"]
    assert stub.requests, "the backend never called a model"


def test_with_no_model_it_says_so_rather_than_inventing_one(client):
    preview = client.post("/api/briefing/preview").json()
    assert preview["ok"] is False
    assert preview["error"]
    # The facts were still gathered — the failure is the narration, not the data.
    assert "facts" in preview


def test_with_no_connectors_chosen_the_turn_gets_no_tools_at_all(client, stub, monkeypatch):
    """The default, and the cheaper path: nothing can be fetched, so nothing can
    be invented. The prompt says exactly that, and no turn is run at all."""
    assert briefing.connector_tool_names(briefing_config.get_config()) == []
    prompt = briefing.facts_to_prompt({}, briefing_config.get_config())
    assert "ONLY facts you have" in prompt
    assert "connected apps" not in prompt

    from jarvis import assembly

    def refuse():
        raise AssertionError("the orchestrator was reached with no connectors chosen")

    monkeypatch.setattr(assembly, "get_orchestrator", refuse)
    stub.says("Good morning.")
    assert client.post("/api/briefing/preview").json()["ok"] is True


def test_choosing_connectors_changes_what_the_prompt_promises(client):
    """With tools on the table, "these are the only facts you have" is no longer
    true, and a stated rule the model can see is false is worse than none."""
    config = {**briefing_config.get_config(), "connectors": ["some-connector"]}
    prompt = briefing.facts_to_prompt({}, config, using_connectors=True)
    assert "connected apps" in prompt
    assert "never a guess about what it might say" in prompt


def test_a_chosen_connector_is_resolved_to_real_tools_at_compose_time(client, stub,
                                                                     monkeypatch):
    """Ids are saved, never names: a connector's tool list changes when it is
    reconnected, so a saved name would go stale in silence.

    What is asserted is the request that actually reached the orchestrator — a
    briefing that composed successfully while quietly passing no allowlist would
    look identical from the outside and be a completely different thing.
    """
    monkeypatch.setattr("jarvis.connectors.capabilities.tool_names_for",
                        lambda connector_id: ["calendar_list_events"])
    client.post("/api/briefing", json={"connectors": ["cal-1"]})

    from jarvis import assembly

    seen: list = []
    real = assembly.get_orchestrator()

    class Watched:
        def run_turn(self, request, *args, **kwargs):
            seen.append(request)
            return real.run_turn(request, *args, **kwargs)

    monkeypatch.setattr(assembly, "get_orchestrator", lambda: Watched())

    stub.says("Good morning. One thing on today.")
    preview = client.post("/api/briefing/preview").json()
    assert preview["ok"] is True
    assert preview["text"] == "Good morning. One thing on today."

    assert len(seen) == 1, "the tool-using path did not run"
    assert seen[0].allowed_names == frozenset({"calendar_list_events"})
    assert seen[0].session_id.startswith("briefing:")  # never bound to chat history


def test_a_stale_connector_id_contributes_nothing_rather_than_breaking(client,
                                                                      monkeypatch):
    monkeypatch.setattr("jarvis.connectors.capabilities.tool_names_for",
                        lambda connector_id: [])
    client.post("/api/briefing", json={"connectors": ["since-removed"]})
    assert briefing.connector_tool_names(briefing_config.get_config()) == []


# --- monitors ----------------------------------------------------------------------

def test_an_empty_install_has_nothing_being_watched(client):
    assert client.get("/api/monitors").json() == {"monitors": []}


def test_a_running_watch_is_listed_with_what_it_is_watching_for(client):
    monitor_store.create_monitor(**WATCH_FOR_A_FILE)
    listed = client.get("/api/monitors").json()["monitors"]
    assert [m["description"] for m in listed] == ["the report landing in Downloads"]
    assert listed[0]["status"] == "watching"


def test_stopping_it_tells_every_open_tab_not_only_the_one_that_clicked(client):
    """A click here and a spoken "stop watching that" must never leave two
    windows disagreeing, which is why this broadcasts rather than just answering."""
    monitor = monitor_store.create_monitor(**WATCH_FOR_A_FILE)
    seen: list[dict] = []
    unsubscribe = bus.subscribe(EventType.MONITOR_STOPPED, lambda event: seen.append(event.payload))
    try:
        assert client.post(f"/api/monitors/{monitor['id']}/stop").json() == {"ok": True}
    finally:
        unsubscribe()

    assert seen and seen[0]["monitorId"] == monitor["id"]
    assert seen[0]["description"] == "the report landing in Downloads"
    assert monitor_store.get_monitor(monitor["id"])["status"] == "stopped"


def test_stopping_something_that_is_not_there_is_a_404(client):
    assert client.post("/api/monitors/nope/stop").status_code == 404


def test_there_is_no_way_to_start_one_from_here(client):
    """Deliberate: starting a watch is the watching capability's job, which works
    out a concrete check from what was actually said. A create endpoint would be
    a way to start one blind."""
    assert client.post("/api/monitors", json=WATCH_FOR_A_FILE).status_code in (404, 405)
