"""The model layer as the app uses it: the Model Settings routes over the config
file, a real turn through the orchestrator, `ai.ask`, the cost ledger, pins,
restarts and the one-time move out of the database — every provider a real
server on a real socket (`stub_wire`)."""

from __future__ import annotations

import json
import sqlite3

import pytest
from starlette.testclient import TestClient

from jarvis import ai, conversation
from jarvis import config as secrets
from jarvis.ai import NoModelAvailable
from jarvis.events import EventType, bus
from jarvis.models import config, engine, execute, state
from jarvis.models.drivers import gemini_generate
from jarvis.models.router import DefaultRouter
from stub_wire import StubWire, Turn

SECRET = "sk-secret-value-1234567890"
TOOLS = [{"name": "get_time", "description": "What time is it.",
          "parameters": {"type": "object", "properties": {"zone": {"type": "string"}}}}]


@pytest.fixture
def client(scratch, monkeypatch):
    from jarvis import assembly
    from jarvis.main import create_app

    monkeypatch.setattr(execute, "sleep", lambda s: None)
    config.forget()
    state.reset()
    engine.router = DefaultRouter()
    assembly.reset_for_tests()
    conversation.reset_for_tests()
    yield TestClient(create_app())
    assembly.reset_for_tests()
    conversation.reset_for_tests()
    config.forget()
    state.reset()


@pytest.fixture
def serve():
    started: list[StubWire] = []

    def make(fmt: str, **kwargs) -> StubWire:
        stub = StubWire(fmt, **kwargs)
        stub.start()
        started.append(stub)
        return stub

    yield make
    for stub in started:
        stub.stop()


def add(client, stub: StubWire, *, key: str | None = None, label: str | None = None, **extra):
    body = {"kind": "custom", "format": stub.fmt, "address": stub.base_url, "apiKey": key, "label": label, **extra}
    reply = client.post("/api/models", json={k: v for k, v in body.items() if v is not None})
    assert reply.status_code == 200, reply.text
    return reply.json()


def select(client, connection_id: str, model_id: str, effort: str | None = None):
    return client.post("/api/models/select", json={"providerId": connection_id, "modelId": model_id,
                                                   "effort": effort})


def connect_and_select(client, serve, fmt: str, *, key: str | None = None, model: str = "stub-a", **stub_kwargs):
    stub = serve(fmt, key=key, **stub_kwargs)
    connection_id = add(client, stub, key=key)["connection"]["id"]
    assert select(client, connection_id, model).status_code == 200
    return stub, connection_id


def run_step(messages=None, *, system="", tools=None, model_id=None, role=None):
    from jarvis.models.client import JarvisModelClient

    events, error = [], None
    try:
        for event in JarvisModelClient().stream(messages=messages or [{"role": "user", "text": "hello"}],
                                                system=system, tools=tools or [], session_id="s1",
                                                model_id=model_id, role=role):
            events.append(event)
    except NoModelAvailable as err:
        error = err
    return events, error


# --- managing connections -------------------------------------------------------------------

def test_a_fresh_install_has_nothing_connected_and_says_so(client):
    body = client.get("/api/models").json()
    assert body["connections"] == [] and body["selection"]["auto"] is True
    assert body["availability"]["state"] == "none"
    assert client.get("/api/status").json() == {"configured": False}


def test_the_picker_offers_presets_from_data_and_formats_by_driver(client):
    body = client.get("/api/models/kinds").json()
    by_id = {k["id"]: k for k in body["kinds"]}
    assert set(by_id) >= {"openai", "anthropic", "gemini", "openrouter", "ollama", "lmstudio", "custom"}
    assert by_id["ollama"]["address"] == "editable" and by_id["ollama"]["key"] == "none"
    assert by_id["custom"]["chooseFormat"] is True and by_id["openai"]["chooseFormat"] is False
    assert {f["id"] for f in body["formats"]} == {"openai_chat", "openai_responses", "anthropic_messages",
                                                  "gemini_generate"}


def test_adding_a_connection_asks_what_it_serves_and_keeps_only_the_keys_name(client, serve):
    stub = serve("anthropic_messages", key=SECRET)
    added = add(client, stub, key=SECRET, label="My Anthropic")
    assert added["tested"]["ok"] and added["discovery"]["added"] == 2
    connection = added["connection"]
    assert connection["id"] == "my-anthropic" and connection["hasKey"] and connection["state"] == "ok"
    assert [m["id"] for m in connection["models"]] == ["stub-a", "stub-b"]
    assert SECRET not in json.dumps(added)
    written = (config.path()).read_text(encoding="utf-8")
    assert SECRET not in written and "secret_ref: model_my_anthropic" in written
    assert stub.requests[0]["headers"]["x-api-key"] == SECRET


def test_a_refused_key_is_reported_in_words_and_the_connection_kept(client, serve):
    stub = serve("openai_chat", key=SECRET)
    added = add(client, stub, key="wrong-key-123456789")
    assert not added["tested"]["ok"] and "didn't accept the key" in added["tested"]["message"]
    assert added["connection"]["state"] == "error" and added["discovery"] is None
    assert "wrong-key-123456789" not in json.dumps(added)
    assert [c["id"] for c in client.get("/api/models").json()["connections"]] == [added["connection"]["id"]]


def test_an_unreachable_server_is_kept_and_says_why(client):
    added = client.post("/api/models", json={"kind": "custom", "format": "openai_chat",
                                             "address": "http://127.0.0.1:9/v1"}).json()
    assert added["ok"] and not added["tested"]["ok"] and "Couldn't reach" in added["tested"]["message"]


def test_a_preset_that_needs_a_key_is_refused_without_one_and_addresses_are_checked_not_rewritten(client):
    assert client.post("/api/models", json={"kind": "openai"}).status_code == 400
    bad = client.post("/api/models", json={"kind": "custom", "format": "openai_chat", "address": "localhost:1"})
    assert bad.status_code == 400 and "web address" in bad.json()["error"]
    kept = client.post("/api/models", json={"kind": "custom", "format": "openai_chat",
                                            "address": "http://127.0.0.1:9/"}).json()
    assert kept["connection"]["address"] == "http://127.0.0.1:9"  # never given a /v1 it wasn't typed with


def test_editing_a_connection_changes_it_and_forgets_what_was_last_tested(client, serve):
    stub = serve("openai_chat")
    connection_id = add(client, stub)["connection"]["id"]
    edited = client.patch(f"/api/models/{connection_id}", json={"label": "Renamed", "apiKey": "new-key"}).json()
    assert edited["connection"]["label"] == "Renamed" and edited["connection"]["state"] == "untested"
    assert edited["connection"]["hasKey"]


def test_deleting_the_selected_connection_removes_its_secret_and_the_selection_is_reported(client, serve):
    stub, connection_id = connect_and_select(client, serve, "openai_chat", key=SECRET)
    ref = config.current().connections[connection_id].secret_ref
    body = client.delete(f"/api/models/{connection_id}").json()
    assert secrets.get_secret(ref) is None
    assert body["selection"]["modelId"] == "stub-a"  # not replaced by anything
    assert body["availability"]["state"] == "missing_connection"
    events, error = run_step()
    assert error is not None and "isn't set up" in str(error)


def test_a_server_with_no_model_list_is_usable_by_naming_the_model(client, serve):
    stub = serve("openai_chat", list_status=404)
    added = add(client, stub)
    connection_id = added["connection"]["id"]
    assert not added["tested"]["ok"] and added["connection"]["models"] == []
    refreshed = client.post(f"/api/models/{connection_id}/discover")
    assert refreshed.status_code == 501 and refreshed.json()["unsupported"]
    client.post(f"/api/models/{connection_id}/models", json={"modelId": "my/model:tag"})
    assert select(client, connection_id, "my/model:tag").status_code == 200
    stub.queue(Turn(text="named by hand"))
    events, error = run_step()
    assert error is None and events[-1].text == "named by hand"
    assert stub.last_body()["model"] == "my/model:tag"


def test_removing_a_model_takes_a_row_off_and_discovery_puts_it_back(client, serve):
    stub = serve("openai_chat")
    connection_id = add(client, stub)["connection"]["id"]
    client.delete(f"/api/models/{connection_id}/models/stub-b")
    assert [m["id"] for m in client.get("/api/models").json()["connections"][0]["models"]] == ["stub-a"]
    client.post(f"/api/models/{connection_id}/discover")
    assert {m["id"] for m in client.get("/api/models").json()["connections"][0]["models"]} == {"stub-a", "stub-b"}


def test_selecting_makes_jarvis_configured_and_effort_is_kept_only_where_it_can_be_used(client, serve):
    stub = serve("anthropic_messages", models=[
        {"id": "thinker", "capabilities": {"effort": {"supported": True}}},
        {"id": "plain", "capabilities": {"effort": {"supported": False}}}])
    connection_id = add(client, stub)["connection"]["id"]
    assert select(client, connection_id, "plain", "high").json()["selection"]["effort"] is None
    assert select(client, connection_id, "thinker", "high").json()["selection"]["effort"] == "high"
    assert client.get("/api/status").json() == {"configured": True}
    run_step()
    assert stub.last_body()["output_config"] == {"effort": "high"}
    levels = {m["id"]: m["effort"] for m in client.get("/api/models").json()["connections"][0]["models"]}
    assert levels["plain"] is None and levels["thinker"]["levels"] == ["none", "low", "medium", "high"]


# --- a turn ---------------------------------------------------------------------------------

def test_a_real_turn_through_the_orchestrator_is_answered_stored_and_costed(client, serve):
    from jarvis import assembly
    from jarvis.orchestrator import TurnRequest
    from jarvis.orchestrator.pipeline import Done, Failed

    stub, _ = connect_and_select(client, serve, "anthropic_messages", model="stub-b")
    stub.queue(Turn(text="Good morning to you", model="stub-b-2026"))
    costs: list[dict] = []
    unsubscribe = bus.subscribe(EventType.MODEL_CALL_COMPLETED, lambda e: costs.append(e.payload))
    try:
        events = list(assembly.get_orchestrator().run_turn(TurnRequest(text="hello there", session_id="s-real")))
    finally:
        if callable(unsubscribe):
            unsubscribe()
    done = [e for e in events if isinstance(e, Done)]
    assert not [e for e in events if isinstance(e, Failed)]
    assert done[0].text == "Good morning to you" and done[0].model_id == "stub-b-2026"
    body = stub.last_body()
    assert body["model"] == "stub-b"
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}  # the stable half, marked
    assert "<identity>" in body["system"][0]["text"]
    last = conversation.get_messages("s-real")[-1]
    assert last["role"] == "assistant" and last["modelId"] == "stub-b-2026"
    assert last["raw"]["layer"] == 1 and any(i["type"] == "sealed" for i in last["raw"]["items"])
    provider_costs = [c for c in costs if c.get("provider")]
    assert provider_costs and provider_costs[-1]["usage"] == {"unitsIn": 12, "unitsOut": 7, "cachedIn": 4}


def test_a_tool_step_round_trips_through_the_stored_conversation(client, serve):
    stub, _ = connect_and_select(client, serve, "gemini_generate")
    stub.queue(Turn(tools=[("get_time", {"zone": "UTC"})]))
    events, error = run_step(tools=TOOLS)
    step = events[-1]
    assert error is None and step.tool_calls[0].args == {"zone": "UTC"}
    history = [{"role": "user", "text": "hello"},
               {"role": "assistant", "toolCalls": [{"id": c.id, "name": c.name, "args": c.args}
                                                   for c in step.tool_calls], "raw": step.raw},
               {"role": "tool", "toolResults": [{"id": step.tool_calls[0].id, "name": "get_time",
                                                 "result": {"time": "noon"}}]}]
    stub.queue(Turn(text="It's noon."))
    events, error = run_step(messages=history, tools=TOOLS)
    assert error is None and events[-1].text == "It's noon."
    parts = [p for c in stub.last_body()["contents"] if c["role"] == "model" for p in c["parts"]]
    assert parts[0]["thoughtSignature"] == "GEMSIG-xyz"  # its own signature, back on its own call


def test_nothing_connected_fails_the_turn_plainly_and_writes_no_reply(client):
    from jarvis import assembly
    from jarvis.orchestrator import TurnRequest
    from jarvis.orchestrator.pipeline import Failed

    events = list(assembly.get_orchestrator().run_turn(TurnRequest(text="hello", session_id="s-none")))
    failures = [e for e in events if isinstance(e, Failed)]
    assert len(failures) == 1 and failures[0].code == "no_model" and "No AI model is connected" in failures[0].error
    assert not [m for m in conversation.get_messages("s-none") if m["role"] == "assistant"]


def test_a_named_model_that_fails_is_never_replaced_and_says_how_to_change_that(client, serve):
    stub, _ = connect_and_select(client, serve, "openai_chat")
    other = serve("openai_chat")
    add(client, other)
    stub.queue(*[Turn(status=503, message="overloaded")] * 3)
    events, error = run_step()
    assert error is not None and "overloaded" in str(error) and "stays on the model you picked" in str(error)
    assert other.generations() == []


def test_under_auto_a_failure_moves_on_and_says_so(client, serve):
    first, second = serve("openai_chat"), serve("openai_chat")
    add(client, first, label="First")
    add(client, second, label="Second")
    client.post("/api/models/select", json={"auto": True})
    first.queue(*[Turn(status=503, message="overloaded")] * 3)
    second.queue(Turn(text="from the second"))
    events, error = run_step()
    from jarvis.orchestrator.model_port import ModelSwitched

    assert error is None and events[-1].text == "from the second"
    moved = [e for e in events if isinstance(e, ModelSwitched)]
    assert moved and moved[0].from_model_id.startswith("first/") and moved[0].to_model_id.startswith("second/")


def test_a_pinned_model_id_becomes_an_alias_on_first_use_and_an_unknown_one_is_refused(client, serve):
    stub = serve("openai_chat")
    connection_id = add(client, stub)["connection"]["id"]
    stub.queue(Turn(text="pinned"))
    events, error = run_step(model_id="stub-b")
    assert error is None and stub.last_body()["model"] == "stub-b"
    assert config.current().aliases["stub-b"].endpoint == f"{connection_id}/stub-b"
    events, error = run_step(model_id="nowhere")
    assert error is not None and "isn't set up on any connection" in str(error)


def test_provider_errors_never_carry_the_key(client, serve):
    stub, _ = connect_and_select(client, serve, "openai_chat", key=SECRET)
    stub.queue(Turn(status=400, message=f"bad request made with key {SECRET}"))
    events, error = run_step()
    assert error is not None and SECRET not in str(error)


def test_a_garbled_tool_request_is_never_run(client, serve):
    from jarvis.models.drivers import openai_chat  # noqa: F401 - the real driver, on a real socket

    stub, _ = connect_and_select(client, serve, "openai_chat")
    stub.queue(Turn(tools=[("get_time", {"zone": "UTC"})]))

    original = StubWire._openai_chat

    def garble(self, body, turn, model):
        # The arguments arrive in pieces; the last is `UTC"}` — drop its closing quote.
        return [f.replace('UTC\\"}', 'UTC}') for f in original(self, body, turn, model)]

    StubWire._openai_chat = garble
    try:
        events, error = run_step(tools=TOOLS)
    finally:
        StubWire._openai_chat = original
    assert error is not None and "garbled" in str(error)


# --- ask --------------------------------------------------------------------------------------

def test_ask_answers_parses_json_and_passes_pictures_along(client, serve):
    stub, _ = connect_and_select(client, serve, "openai_responses")
    stub.queue(Turn(text="Four.", reasoning=False))
    assert ai.ask("2+2?", data_class="public", task_class="judge").text == "Four."
    stub.queue(Turn(text='{"matches": true}', reasoning=False))
    answer = ai.ask("does it match?", data_class="personal", task_class="judge", want_json=True)
    assert answer.data == {"matches": True}
    stub.queue(Turn(text="A square.", reasoning=False))
    ai.ask("what is this?", data_class="sensitive", task_class="vision",
           media=[{"kind": "image", "mimeType": "image/png", "dataBase64": "AAAA"}], need={"vision": True})
    assert '"input_image"' in json.dumps(stub.last_body())


def test_ask_model_reports_nothing_connected_as_a_value(client):
    reply = ai.ask_model("hi", data_class="personal", task_class="judge", background=True)
    assert reply.ok is False and "No AI model is connected" in reply.error


def test_a_configured_privacy_restriction_holds_for_ask_too(client, serve):
    stub, connection_id = connect_and_select(client, serve, "openai_chat")
    config.edit(lambda d: d.update({"policies": {"data_classes": {"sensitive": ["local"]}}}))
    with pytest.raises(NoModelAvailable, match="privacy settings"):
        ai.ask("look at my screen", data_class="sensitive", task_class="screen_check")
    assert stub.generations() == []


# --- surviving ----------------------------------------------------------------------------------

def test_connections_the_selection_and_the_key_survive_a_restart(client, serve):
    from jarvis import assembly, db
    from jarvis.main import create_app

    stub = serve("anthropic_messages", key=SECRET, models=[{"id": "thinker", "capabilities": {
        "effort": {"supported": True}}}])
    connection_id = add(client, stub, key=SECRET)["connection"]["id"]
    select(client, connection_id, "thinker", "high")
    before = client.get("/api/models").json()
    state.flush()

    db.reset_for_tests()
    assembly.reset_for_tests()
    config.forget()
    state.reset()
    after = TestClient(create_app()).get("/api/models").json()
    assert after == before
    run_step()
    assert stub.last_body()["model"] == "thinker" and stub.last_body()["output_config"] == {"effort": "high"}
    assert stub.generations()[-1]["headers"]["x-api-key"] == SECRET


def test_the_old_tables_move_out_during_migration_and_are_dropped(scratch):
    """Migration 35 on a database that still has the old model tables."""
    from jarvis import db

    path = scratch.data_dir / "jarvis.db"
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    original = db.MIGRATION_COUNT
    try:
        db.MIGRATION_COUNT = 34
        db.migrate(conn)
    finally:
        db.MIGRATION_COUNT = original
    conn.execute("INSERT INTO model_providers (id, kind, format, label, base_url, secret_ref, created_at) "
                 "VALUES ('prov_1', 'lmstudio', 'openai-chat', 'LM Studio', NULL, NULL, '2026-01-01')")
    conn.execute("INSERT INTO provider_models (provider_id, model_id, label, source, facts_json, added_at) "
                 "VALUES ('prov_1', 'qwen', NULL, 'discovered', NULL, '2026-01-02')")
    config.forget()
    state.reset()
    db.migrate(conn)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not {"model_providers", "provider_models", "model_outcomes"} & tables
    assert "model_traces" in tables
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.MIGRATION_COUNT
    moved = config.current().connections["lm-studio"]
    assert moved.trust == "local" and moved.base_url == "http://127.0.0.1:1234/v1" and moved.quirks == "lmstudio"
    assert [d.model_id for d in state.discovered("lm-studio")] == ["qwen"]
    conn.close()


# --- small guarantees ---------------------------------------------------------------------------

def _arrays_without_items(schema) -> bool:
    if isinstance(schema, dict):
        if schema.get("type") == "array" and "items" not in schema:
            return True
        return any(_arrays_without_items(v) for v in schema.values())
    if isinstance(schema, list):
        return any(_arrays_without_items(v) for v in schema)
    return False


def test_every_tool_the_app_really_declares_is_acceptable_to_gemini(scratch):
    """Found live, not by a stub: Google refuses an array that doesn't say what it
    holds — and the whole request with it, so every turn that declared the tool."""
    from jarvis import assembly

    assembly.reset_for_tests()
    try:
        registry = assembly.get_registry()
        declared = registry.declarations(registry.list())
        assert len(declared) > 20
        own = [f"{d['name']}.{arg}" for d in declared
               for arg, schema in (d["parameters"].get("properties") or {}).items()
               if isinstance(schema, dict) and schema.get("type") == "array" and not schema.get("items")]
        assert own == [], f"tool arguments that are an array with no element type: {own}"
        sent = [(d["name"], gemini_generate.translate_schema(d["parameters"])) for d in declared]
        assert [n for n, schema in sent if _arrays_without_items(schema)] == []
    finally:
        assembly.reset_for_tests()
