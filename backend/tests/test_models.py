"""The provider and model system, against real HTTP.

Every provider here is a real server on a real socket (`stub_provider_server`),
speaking its format's genuine shapes including streamed events — the only stand-in
is which company is on the far end. What is asserted is what actually went on the
wire (the model id, the key, the effort), because that is the difference between a
model system that works and one that reports that it does.

The tests are grouped by the question they answer: can a connection be managed;
are testing, discovering and running kept apart; is a selection reported honestly
rather than quietly replaced; and does a request reach the right provider, with the
right model, and come back as what really happened.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest
from starlette.testclient import TestClient

from jarvis import ai, config, conversation
from jarvis.ai import NoModelAvailable
from jarvis.events import EventType, bus
from jarvis.models import kinds, selection, store
from jarvis.prompt_format import CACHE_BREAK
from stub_provider_server import FORMATS, StubProvider

SECRET = "sk-secret-value-1234567890"
TOOLS = [{"name": "get_time", "description": "What time is it.",
          "parameters": {"type": "object", "properties": {"zone": {"type": "string"}}}}]


@pytest.fixture
def client(scratch, monkeypatch):
    from jarvis import assembly
    from jarvis.main import create_app
    from jarvis.models import attempt

    monkeypatch.setattr(attempt, "_BUSY_WAITS_S", (0.0, 0.0))  # the retry is real; its pause need not be
    assembly.reset_for_tests()
    conversation.reset_for_tests()
    yield TestClient(create_app())
    assembly.reset_for_tests()
    conversation.reset_for_tests()


@pytest.fixture
def serve():
    started: list[StubProvider] = []

    def make(format: str, **kwargs) -> StubProvider:
        stub = StubProvider(format, **kwargs)
        stub.start()
        started.append(stub)
        return stub

    yield make
    for stub in started:
        stub.stop()


def add(client, stub: StubProvider, *, key: str | None = None, label: str | None = None,
        kind: str = "custom", **extra):
    """Connect `stub` the way a person would: a custom connection at its address."""
    body = {"kind": kind, "address": stub.base_url, "apiKey": key, "label": label, **extra}
    if kind == "custom":
        body["format"] = stub.format
    reply = client.post("/api/models", json={k: v for k, v in body.items() if v is not None})
    assert reply.status_code == 200, reply.text
    return reply.json()


def select(client, connection_id: str, model_id: str, effort: str | None = None):
    return client.post("/api/models/select", json={"providerId": connection_id, "modelId": model_id,
                                                   "effort": effort})


def connect_and_select(client, serve, format: str, *, key: str | None = None, model: str | None = None, **stub_kwargs):
    stub = serve(format, key=key, **stub_kwargs)
    added = add(client, stub, key=key)
    connection_id = added["connection"]["id"]
    model_id = model or added["connection"]["models"][0]["id"]
    assert select(client, connection_id, model_id).status_code == 200
    return stub, connection_id, model_id


def run_step(messages=None, *, system="", tools=None, model_id=None, need=None, role=None):
    """One step through the turn loop's own client. Returns (events, error)."""
    from jarvis.models.client import JarvisModelClient

    events, error = [], None
    try:
        for event in JarvisModelClient().stream(
                messages=messages or [{"role": "user", "text": "hello"}], system=system,
                tools=tools or [], session_id="s1", model_id=model_id, role=role, need=need):
            events.append(event)
    except NoModelAvailable as err:
        error = err
    return events, error


def wire_model(stub: StubProvider) -> str:
    request = stub.posts()[-1]
    if stub.format == "gemini-generatecontent":
        return request["path"].rsplit("/models/", 1)[-1].split(":", 1)[0]
    return request["body"]["model"]


ANTHROPIC_OPUS = {"id": "opus-x", "display_name": "Opus X", "max_tokens": 128000, "capabilities": {
    "effort": {"supported": True, "low": {"supported": True}, "medium": {"supported": True},
               "high": {"supported": True}, "max": {"supported": True}, "xhigh": None},
    "thinking": {"supported": True}}}
ANTHROPIC_PLAIN = {"id": "plain-x", "max_tokens": 8192, "capabilities": {
    "effort": {"supported": False}, "thinking": {"supported": False}}}


# ================================================================================
# Managing a connection
# ================================================================================

def test_a_fresh_install_has_nothing_connected_and_says_so(client):
    body = client.get("/api/models").json()
    assert body["connections"] == []
    assert body["availability"]["state"] == "none"
    assert client.get("/api/status").json() == {"configured": False}


def test_every_kind_offered_in_the_interface_has_a_real_implementation():
    """The correspondence rule: nothing is listed that the backend cannot run."""
    from jarvis.models import providers

    for kind in kinds.KINDS.values():
        if kind.format:
            assert providers.for_format(kind.format).FORMAT == kind.format
    for format_id in kinds.FORMATS:
        module = providers.for_format(format_id)
        assert callable(module.check) and callable(module.discover) and callable(module.stream)


def test_adding_a_connection_tests_it_and_lists_what_it_offers(client, serve):
    stub = serve("openai-chat")
    added = add(client, stub)
    assert added["tested"]["ok"] is True
    assert added["discovery"]["ok"] is True and added["discovery"]["added"] == 2
    connection = added["connection"]
    assert connection["state"] == "ok"
    assert [m["id"] for m in connection["models"]] == ["stub-model-a", "stub-model-b"]
    assert connection["address"] == stub.base_url


@pytest.mark.parametrize("format", FORMATS)
def test_a_key_is_sent_the_way_each_provider_expects_and_never_echoed(client, serve, format):
    stub = serve(format, key=SECRET)
    added = add(client, stub, key=SECRET)
    assert added["tested"]["ok"] is True, added["tested"]
    sent = stub.requests[0]["headers"]
    assert {"openai-responses": sent.get("authorization") == f"Bearer {SECRET}",
            "openai-chat": sent.get("authorization") == f"Bearer {SECRET}",
            "anthropic-messages": sent.get("x-api-key") == SECRET,
            "gemini-generatecontent": sent.get("x-goog-api-key") == SECRET}[format]
    # Gemini's key is kept out of the URL, where a log or an error would carry it.
    assert SECRET not in stub.requests[0]["path"] and not stub.requests[0]["query"].get("key")

    everything = json.dumps([added, client.get("/api/models").json(),
                             client.get("/api/models/kinds").json()])
    assert SECRET not in everything
    assert added["connection"]["hasKey"] is True
    assert config.get_secret(f"model_{added['connection']['id']}") == SECRET
    # ...nor does it sit in the database row: only the NAME of the secret does.
    from jarvis.db import get_db

    dump = json.dumps([dict(r) for r in get_db().execute("SELECT * FROM model_providers")])
    assert SECRET not in dump


def test_a_wrong_key_is_reported_in_words_and_marks_the_connection_as_needing_attention(client, serve):
    stub = serve("openai-chat", key=SECRET)
    added = add(client, stub, key="not-the-key")
    assert added["tested"]["ok"] is False
    assert "didn't accept the key" in added["tested"]["message"]
    assert added["connection"]["state"] == "error"
    assert added["discovery"] is None  # nothing to discover through a connection that was refused
    assert "not-the-key" not in json.dumps(added)


def test_an_unreachable_server_is_kept_and_says_why(client):
    reply = client.post("/api/models", json={"kind": "custom", "format": "openai-chat",
                                             "address": "http://127.0.0.1:9/v1"}).json()
    assert reply["ok"] is True
    assert reply["tested"]["ok"] is False and "Couldn't reach" in reply["tested"]["message"]
    assert reply["connection"]["state"] == "error"


def test_a_provider_that_needs_a_key_is_refused_without_one(client):
    reply = client.post("/api/models", json={"kind": "anthropic"})
    assert reply.status_code == 400 and "needs an API key" in reply.json()["error"]
    assert client.get("/api/models").json()["connections"] == []


def test_addresses_are_checked_and_local_servers_get_their_v1():
    assert kinds.normalize_base_url("ollama", "localhost:11434") == "http://localhost:11434/v1"
    assert kinds.normalize_base_url("lmstudio", "http://127.0.0.1:1234/") == "http://127.0.0.1:1234/v1"
    assert kinds.normalize_base_url("custom", "https://example.com/api/v1/") == "https://example.com/api/v1"
    # A provider with a fixed address never stores one — a stale address there is invisible.
    assert kinds.normalize_base_url("openai", "http://evil.example") is None
    with pytest.raises(kinds.InvalidAddress):
        kinds.normalize_base_url("custom", "")
    with pytest.raises(kinds.InvalidAddress):
        kinds.normalize_base_url("custom", "ftp://nope")


def test_editing_a_connection_changes_it_and_forgets_what_was_last_tested(client, serve):
    stub = serve("openai-chat", key=SECRET)
    connection_id = add(client, stub, key=SECRET)["connection"]["id"]
    edited = client.patch(f"/api/models/{connection_id}", json={"label": "Work", "apiKey": "new-key-9999"}).json()
    assert edited["connection"]["label"] == "Work"
    assert edited["connection"]["state"] == "untested"  # the last test no longer describes it
    assert config.get_secret(f"model_{connection_id}") == "new-key-9999"
    assert client.patch(f"/api/models/{connection_id}", json={"label": "  "}).status_code == 400
    assert client.patch("/api/models/prov_nope", json={"label": "x"}).status_code == 404


def test_deleting_a_connection_removes_its_models_and_its_secret(client, serve):
    stub = serve("openai-chat", key=SECRET)
    connection_id = add(client, stub, key=SECRET)["connection"]["id"]
    assert client.delete(f"/api/models/{connection_id}").json()["ok"] is True
    assert client.get("/api/models").json()["connections"] == []
    assert store.list_models() == []
    assert config.get_secret(f"model_{connection_id}") in (None, "")
    assert client.delete(f"/api/models/{connection_id}").status_code == 404


# ================================================================================
# Testing, discovering and running are three different questions
# ================================================================================

def test_a_failed_discovery_does_not_change_the_connections_status(client, serve):
    stub = serve("openai-chat")
    connection_id = add(client, stub)["connection"]["id"]
    assert store.get_connection(connection_id).state == "ok"
    stub.list_status = 500
    reply = client.post(f"/api/models/{connection_id}/discover")
    assert reply.status_code == 502 and reply.json()["unsupported"] is False
    assert store.get_connection(connection_id).state == "ok"  # discovery is not the test
    assert len(store.list_models(connection_id)) == 2  # and it took nothing away


def test_a_provider_with_no_model_list_is_still_usable_by_naming_the_model(client, serve):
    stub = serve("openai-chat", list_status=404)
    added = add(client, stub)
    assert added["tested"]["ok"] is True
    assert "doesn't offer a list" in added["tested"]["message"]  # reachable, just not listable
    assert added["discovery"]["unsupported"] is True
    connection_id = added["connection"]["id"]
    assert added["connection"]["models"] == []

    typed = client.post(f"/api/models/{connection_id}/models", json={"modelId": "my-private-model"}).json()
    assert [(m["id"], m["source"]) for m in typed["connection"]["models"]] == [("my-private-model", "manual")]
    assert select(client, connection_id, "my-private-model").status_code == 200
    events, error = run_step()
    assert error is None and wire_model(stub) == "my-private-model"


def test_a_discovery_that_fails_never_blocks_adding_a_model_by_hand(client, serve):
    stub = serve("openai-chat", list_status=500)
    added = add(client, stub)
    assert added["tested"]["ok"] is False  # the list is how this server is checked, and it is down
    connection_id = added["connection"]["id"]
    typed = client.post(f"/api/models/{connection_id}/models", json={"modelId": "hand-typed"})
    assert typed.status_code == 200
    assert client.post(f"/api/models/{connection_id}/models", json={"modelId": " "}).status_code == 400


def test_a_wrong_address_is_told_apart_from_a_server_with_no_model_list(client, serve):
    """A 404 on the model list could be either. The chat endpoint settles which."""
    stub = serve("openai-chat", list_status=404)
    stub.base_url = stub.base_url.rsplit("/v1", 1)[0] + "/nope"  # nothing answers under /nope
    reply = client.post("/api/models", json={"kind": "custom", "format": "openai-chat",
                                             "address": stub.base_url}).json()
    assert reply["tested"]["ok"] is False and "Check the address" in reply["tested"]["message"]


def test_a_restricted_openai_key_that_cannot_list_models_still_connects(client, serve):
    stub = serve("openai-responses", key=SECRET, list_status=403)
    added = add(client, stub, key=SECRET)
    assert added["tested"]["ok"] is True and "isn't allowed to list models" in added["tested"]["message"]
    assert added["discovery"]["unsupported"] is True


def test_removing_a_model_takes_a_row_off_the_list_and_discovery_puts_it_back(client, serve):
    stub = serve("openai-chat")
    connection_id = add(client, stub)["connection"]["id"]
    assert client.delete(f"/api/models/{connection_id}/models/stub-model-a").json()["ok"] is True
    assert [m.model_id for m in store.list_models(connection_id)] == ["stub-model-b"]
    client.post(f"/api/models/{connection_id}/discover")
    assert {m.model_id for m in store.list_models(connection_id)} == {"stub-model-a", "stub-model-b"}
    assert client.delete(f"/api/models/{connection_id}/models/never-there").status_code == 404


def test_model_ids_with_slashes_and_colons_survive_the_round_trip(client, serve):
    stub = serve("openai-chat", models=[{"id": "meta-llama/Llama-3.1-8B:latest"}])
    connection_id = add(client, stub)["connection"]["id"]
    assert client.delete(f"/api/models/{connection_id}/models/meta-llama/Llama-3.1-8B:latest").status_code == 200


def test_a_model_the_provider_stops_listing_is_noted_but_stays_usable(client, serve):
    """A provider's list is not the last word on what it will run."""
    stub = serve("openai-chat")
    connection_id = add(client, stub)["connection"]["id"]
    stub.models = [{"id": "stub-model-b"}]
    client.post(f"/api/models/{connection_id}/discover")

    by_id = {m["id"]: m for m in client.get("/api/models").json()["connections"][0]["models"]}
    assert by_id["stub-model-a"]["stillListed"] is False and by_id["stub-model-b"]["stillListed"] is True
    assert select(client, connection_id, "stub-model-a").status_code == 200
    events, error = run_step()
    assert error is None and wire_model(stub) == "stub-model-a"


def test_gemini_discovery_lists_only_what_google_says_can_generate(client, serve):
    stub = serve("gemini-generatecontent", models=[
        {"id": "gemini-chatty"}, {"id": "text-embedder", "methods": ["embedContent"]}])
    assert [m["id"] for m in add(client, stub)["connection"]["models"]] == ["gemini-chatty"]


def test_anthropic_discovery_keeps_only_what_the_provider_reported(client, serve):
    stub = serve("anthropic-messages", models=[ANTHROPIC_OPUS, ANTHROPIC_PLAIN])
    models = {m["id"]: m for m in add(client, stub)["connection"]["models"]}
    assert models["opus-x"]["effort"] == {"levels": ["low", "medium", "high", "max"], "default": "high"}
    assert models["plain-x"]["effort"] is None  # not reported, so not offered and not assumed
    assert store.get_model(add_id(client), "opus-x").facts["maxOutput"] == 128000


def add_id(client) -> str:
    return client.get("/api/models").json()["connections"][0]["id"]


# ================================================================================
# Selection is the person's; availability is reported; nothing is quietly swapped
# ================================================================================

def test_selecting_a_model_makes_jarvis_configured(client, serve):
    stub = serve("openai-chat")
    connection_id = add(client, stub)["connection"]["id"]
    assert client.get("/api/status").json() == {"configured": False}  # connected is not chosen
    assert select(client, connection_id, "stub-model-b").json()["availability"]["state"] == "ok"
    assert client.get("/api/status").json() == {"configured": True}
    assert select(client, connection_id, "not-a-model").status_code == 404
    assert select(client, "prov_nope", "stub-model-a").status_code == 404


def test_deleting_the_selected_connection_leaves_the_selection_reported_and_not_replaced(client, serve):
    chosen = serve("openai-chat", reply="from the chosen one")
    other = serve("anthropic-messages", reply="from the OTHER one")
    chosen_id = add(client, chosen)["connection"]["id"]
    add(client, other)
    select(client, chosen_id, "stub-model-a")
    client.delete(f"/api/models/{chosen_id}")

    body = client.get("/api/models").json()
    assert body["selection"]["modelId"] == "stub-model-a"  # kept, not reassigned
    assert body["availability"]["state"] == "missing_connection"
    assert "stub-model-a" in body["availability"]["message"] and "removed" in body["availability"]["message"]
    assert client.get("/api/status").json() == {"configured": False}

    events, error = run_step()
    assert events == [] and error is not None and "removed" in str(error)
    # The other connection was perfectly good — and was not asked. That is the point.
    assert other.posts() == []


def test_a_removed_model_row_is_reported_by_name_and_not_replaced(client, serve):
    stub = serve("openai-chat")
    connection_id = add(client, stub)["connection"]["id"]
    select(client, connection_id, "stub-model-a")
    client.delete(f"/api/models/{connection_id}/models/stub-model-a")
    state = client.get("/api/models").json()["availability"]
    assert state["state"] == "missing_model" and "stub-model-a" in state["message"]
    events, error = run_step()
    assert error is not None and stub.posts() == []  # not run on stub-model-b


def test_a_selection_whose_key_has_gone_is_unavailable_and_says_which(client, serve):
    stub = serve("anthropic-messages", key=SECRET)
    connection_id = add(client, stub, key=SECRET, kind="custom")["connection"]["id"]
    # Custom keys are optional, so use a kind that needs one: swap the row's kind in place.
    from jarvis.db import get_db

    get_db().execute("UPDATE model_providers SET kind = 'anthropic', base_url = ? WHERE id = ?",
                     (stub.base_url, connection_id))
    select(client, connection_id, add_first_model(connection_id))
    config.delete_secret(f"model_{connection_id}")
    state = client.get("/api/models").json()["availability"]
    assert state["state"] == "no_key" and "no key saved" in state["message"]
    assert stub.posts() == []


def add_first_model(connection_id: str) -> str:
    return store.list_models(connection_id)[0].model_id


def test_effort_is_kept_only_for_a_model_the_provider_reported_levels_for(client, serve):
    stub = serve("anthropic-messages", models=[ANTHROPIC_OPUS, ANTHROPIC_PLAIN])
    connection_id = add(client, stub)["connection"]["id"]
    assert select(client, connection_id, "opus-x", "low").json()["selection"]["effort"] == "low"
    assert select(client, connection_id, "opus-x", "extreme").json()["selection"]["effort"] is None
    # Moving to a model with no levels drops the effort rather than carrying it over.
    assert select(client, connection_id, "plain-x", "low").json()["selection"]["effort"] is None


def test_a_pinned_model_that_cannot_be_found_is_an_error_not_a_reason_to_pick_another(client, serve):
    stub = serve("openai-chat")
    connection_id = add(client, stub)["connection"]["id"]
    select(client, connection_id, "stub-model-a")
    events, error = run_step(model_id="some-model-that-is-not-here")
    assert error is not None and "some-model-that-is-not-here" in str(error) and stub.posts() == []
    events, error = run_step(model_id="stub-model-b")
    assert error is None and wire_model(stub) == "stub-model-b"


def test_web_search_through_the_model_is_refused_rather_than_answered_from_memory(client, serve):
    stub, *_ = connect_and_select(client, serve, "openai-chat")
    events, error = run_step(need={"webSearch": True})
    assert error is not None and "wasn't done" in str(error) and stub.posts() == []


# ================================================================================
# Running: the right provider, the right model, and what really came back
# ================================================================================

@pytest.mark.parametrize("format", FORMATS)
def test_a_turn_reaches_the_selected_model_and_streams_its_reply(client, serve, format):
    stub, _, model_id = connect_and_select(client, serve, format, model="stub-model-b",
                                           reply="Hello there, this is a reply")
    events, error = run_step()
    assert error is None
    from jarvis.orchestrator.model_port import StepComplete, TextChunk

    chunks = [e.text for e in events if isinstance(e, TextChunk)]
    assert len(chunks) > 1 and "".join(chunks) == "Hello there, this is a reply"  # really streamed
    step = events[-1]
    assert isinstance(step, StepComplete) and step.text == "Hello there, this is a reply"
    assert step.finish_reason == "stop" and step.tool_calls == ()
    assert wire_model(stub) == "stub-model-b"  # THE model, on the wire — not a default, not another
    assert step.model_id == "stub-model-b"


@pytest.mark.parametrize("format", FORMATS)
def test_a_tool_call_comes_back_whole_and_its_result_goes_back_in_that_providers_format(client, serve, format):
    stub, *_ = connect_and_select(client, serve, format, tool=("get_time", {"zone": "UTC"}), reply="It is noon.")
    first, error = run_step(tools=TOOLS)
    assert error is None
    step = first[-1]
    assert step.finish_reason == "tool_calls"
    assert [(c.name, c.args) for c in step.tool_calls] == [("get_time", {"zone": "UTC"})]  # rebuilt from fragments
    call = step.tool_calls[0]

    history = [{"role": "user", "text": "what time is it"},
               {"role": "assistant", "text": "", "modelId": step.model_id, "raw": step.raw,
                "toolCalls": [{"id": call.id, "name": call.name, "args": call.args}]},
               {"role": "tool", "toolResults": [{"id": call.id, "name": call.name, "result": {"time": "12:00"}}]}]
    second, error = run_step(history, tools=TOOLS)
    assert error is None and second[-1].text == "It is noon."
    sent = json.dumps(stub.last_body())
    assert "12:00" in sent  # the result really was handed back
    assert {"openai-responses": '"function_call_output"', "openai-chat": '"role": "tool"',
            "anthropic-messages": '"tool_result"', "gemini-generatecontent": '"functionResponse"'}[format] in sent


def test_anthropic_thinking_blocks_are_replayed_untouched_inside_a_tool_loop_and_dropped_after_it(client, serve):
    stub, *_ = connect_and_select(client, serve, "anthropic-messages", tool=("get_time", {"zone": "UTC"}))
    step = run_step(tools=TOOLS)[0][-1]
    assert [b["type"] for b in step.raw["content"]] == ["thinking", "tool_use"]
    call = step.tool_calls[0]
    loop = [{"role": "user", "text": "time?"},
            {"role": "assistant", "text": "", "raw": step.raw, "toolCalls": [{"id": call.id, "name": call.name,
                                                                              "args": call.args}]},
            {"role": "tool", "toolResults": [{"id": call.id, "name": call.name, "result": {"t": 1}}]}]
    run_step(loop, tools=TOOLS)
    replayed = stub.last_body()["messages"][1]["content"]
    assert replayed[0] == {"type": "thinking", "thinking": "", "signature": "SIG-abc123"}  # verbatim
    assert replayed[1]["type"] == "tool_use" and replayed[1]["id"] == "toolu_stub1"

    # Once the person has spoken again, that loop is finished, and its thinking may be left out.
    later = loop + [{"role": "assistant", "text": "It is 1."}, {"role": "user", "text": "thanks"}]
    run_step(later, tools=TOOLS)
    assert all(b["type"] != "thinking" for b in stub.last_body()["messages"][1]["content"])


def test_gemini_thought_signatures_are_replayed_verbatim(client, serve):
    stub, *_ = connect_and_select(client, serve, "gemini-generatecontent", tool=("get_time", {"zone": "UTC"}))
    step = run_step(tools=TOOLS)[0][-1]
    call = step.tool_calls[0]
    run_step([{"role": "user", "text": "time?"},
              {"role": "assistant", "text": "", "raw": step.raw,
               "toolCalls": [{"id": call.id, "name": call.name, "args": call.args}]},
              {"role": "tool", "toolResults": [{"id": call.id, "name": call.name, "result": {"t": 1}}]}], tools=TOOLS)
    model_turn = stub.last_body()["contents"][1]
    assert model_turn["role"] == "model"
    assert model_turn["parts"][0]["thoughtSignature"] == "GEMSIG-xyz"


def test_history_written_by_one_provider_is_rebuilt_for_another_rather_than_replayed(client, serve):
    """Switching models mid-conversation: another provider's raw reply is never sent on."""
    stub, *_ = connect_and_select(client, serve, "anthropic-messages")
    foreign = {"adapter": "gemini-generatecontent", "parts": [{"text": "SECRET-GEMINI-INTERNALS"}]}
    run_step([{"role": "user", "text": "hi"}, {"role": "assistant", "text": "hello", "raw": foreign},
              {"role": "user", "text": "again"}])
    sent = json.dumps(stub.last_body())
    assert "SECRET-GEMINI-INTERNALS" not in sent and "hello" in sent


def test_the_cache_marker_never_reaches_a_provider_as_text(client, serve):
    system = "STABLE PART" + CACHE_BREAK + "VOLATILE PART"
    for format in ("openai-responses", "openai-chat", "gemini-generatecontent"):
        stub, *_ = connect_and_select(client, serve, format)
        run_step(system=system)
        sent = json.dumps(stub.last_body())
        assert "<<<volatile>>>" not in sent and "STABLE PART" in sent and "VOLATILE PART" in sent
    stub, *_ = connect_and_select(client, serve, "anthropic-messages")
    run_step(system=system)
    blocks = stub.last_body()["system"]
    assert [b["text"] for b in blocks] == ["STABLE PART", "VOLATILE PART"]
    assert blocks[0]["cache_control"] == {"type": "ephemeral"} and "cache_control" not in blocks[1]


def test_openai_is_sent_its_own_format_with_nothing_stored_and_no_strict_schemas(client, serve):
    stub, *_ = connect_and_select(client, serve, "openai-responses")
    run_step(tools=TOOLS, system="be brief")
    body = stub.last_body()
    assert stub.posts()[-1]["path"].endswith("/responses")
    assert body["store"] is False and body["instructions"] == "be brief"
    assert body["tools"][0]["strict"] is False and "reasoning" not in body


def test_effort_reaches_anthropic_only_for_a_model_reported_to_accept_it(client, serve):
    stub = serve("anthropic-messages", models=[ANTHROPIC_OPUS, ANTHROPIC_PLAIN])
    connection_id = add(client, stub)["connection"]["id"]

    select(client, connection_id, "opus-x", "low")
    run_step()
    body = stub.last_body()
    assert body["output_config"] == {"effort": "low"} and "thinking" not in body  # alone; nothing forced on
    assert body["max_tokens"] == 64000  # min(the model's own 128000, what a request wants)

    select(client, connection_id, "opus-x", None)
    run_step()
    assert "output_config" not in stub.last_body()

    select(client, connection_id, "plain-x", "low")
    run_step()
    body = stub.last_body()
    assert "output_config" not in body and body["max_tokens"] == 8192  # the model's own ceiling, not a guess


def test_a_model_added_by_hand_gets_a_ceiling_any_current_model_accepts(client, serve):
    stub = serve("anthropic-messages", models=[])
    connection_id = add(client, stub)["connection"]["id"]
    client.post(f"/api/models/{connection_id}/models", json={"modelId": "brand-new-model"})
    select(client, connection_id, "brand-new-model")
    run_step()
    assert stub.last_body()["max_tokens"] == 16384 and "output_config" not in stub.last_body()


@pytest.mark.parametrize("format", FORMATS)
def test_a_model_the_provider_does_not_know_fails_the_turn_in_the_providers_words(client, serve, format):
    stub, connection_id, _ = connect_and_select(client, serve, format, unknown_model="stub-model-a")
    events, error = run_step()
    assert events == [] and error is not None
    assert "does not exist" in str(error) and "stub-model-a" in str(error)  # the provider's own words
    # A working connection can still refuse one model: its status is not touched by a run.
    assert store.get_connection(connection_id).state == "ok"


@pytest.mark.parametrize("format", FORMATS)
def test_a_key_that_stops_working_fails_the_turn_and_does_not_rewrite_the_connections_status(client, serve, format):
    stub, connection_id, _ = connect_and_select(client, serve, format, key=SECRET)
    stub.key = "a-different-key-now"
    events, error = run_step()
    assert error is not None and "didn't accept the key" in str(error)
    assert SECRET not in str(error)
    assert store.get_connection(connection_id).state == "ok"


@pytest.mark.parametrize("format", FORMATS)
def test_a_reply_that_just_stops_is_an_error_and_not_a_finished_answer(client, serve, format):
    connect_and_select(client, serve, format, truncate=True)
    events, error = run_step()
    assert error is not None and "stopped part-way" in str(error)
    from jarvis.orchestrator.model_port import StepComplete

    assert not any(isinstance(e, StepComplete) for e in events)


@pytest.mark.parametrize("format", FORMATS)
def test_a_server_error_is_reported_and_never_a_success(client, serve, format):
    stub, *_ = connect_and_select(client, serve, format)
    stub.chat_status = 503
    events, error = run_step()
    assert error is not None and "problem on its end" in str(error)


def test_a_different_model_answering_is_surfaced_and_the_reported_one_is_recorded(client, serve):
    from jarvis.orchestrator.model_port import ModelSwitched, StepComplete

    stub, *_ = connect_and_select(client, serve, "openai-chat", model="stub-model-a", answers_as="some-other-model")
    events, _ = run_step()
    switched = [e for e in events if isinstance(e, ModelSwitched)]
    assert len(switched) == 1 and switched[0].from_model_id == "stub-model-a"
    assert switched[0].to_model_id == "some-other-model"
    assert events[-1].model_id == "some-other-model"  # what answered — not what was asked for


def test_a_dated_snapshot_of_the_model_asked_for_is_not_a_different_model(client, serve):
    from jarvis.orchestrator.model_port import ModelSwitched

    connect_and_select(client, serve, "openai-chat", model="stub-model-a", answers_as="stub-model-a-2026-01-01")
    events, _ = run_step()
    assert not any(isinstance(e, ModelSwitched) for e in events) and events[-1].model_id == "stub-model-a-2026-01-01"


def test_the_client_never_switches_models_on_its_own(client, serve):
    """Two working models, the selected one failing: the other is never tried."""
    stub, connection_id, _ = connect_and_select(client, serve, "openai-chat", model="stub-model-a",
                                                unknown_model="stub-model-a")
    events, error = run_step()
    assert error is not None
    assert [r["body"]["model"] for r in stub.posts()] == ["stub-model-a"]  # exactly one attempt, on that model


@pytest.mark.parametrize("format", FORMATS)
def test_what_a_call_used_reaches_the_cost_ledger_as_the_provider_reported_it(client, serve, format):
    seen = []
    unsubscribe = bus.subscribe(EventType.MODEL_CALL_COMPLETED, lambda event: seen.append(event.payload))
    try:
        connect_and_select(client, serve, format)
        run_step()
    finally:
        unsubscribe()
    reported = [p for p in seen if p.get("provider")]
    assert len(reported) == 1
    payload = reported[0]
    assert payload["provider"] == "custom" and payload["usage"]["unitsOut"] == 7
    assert payload["usage"]["cachedIn"] == 4 and payload["usage"]["unitsIn"] >= 12


# ================================================================================
# The one-shot path tools and background work use
# ================================================================================

def test_ask_answers_from_the_selected_model(client, serve):
    stub, *_ = connect_and_select(client, serve, "anthropic-messages", reply="Forty-two")
    answer = ai.ask("What is the answer?", system="be brief")
    assert answer.text == "Forty-two" and answer.model_id == "stub-model-a"
    assert stub.last_body()["messages"] == [{"role": "user", "content": [{"type": "text", "text": "What is the answer?"}]}]


def test_ask_reads_json_out_of_a_reply_that_wrapped_it(client, serve):
    connect_and_select(client, serve, "openai-chat", reply='```json {"matches": true} ```')
    assert ai.ask("check", want_json=True).data == {"matches": True}
    assert ai.ask("check").data is None  # only when asked for


def test_ask_json_that_is_not_json_is_none_and_not_invented(client, serve):
    connect_and_select(client, serve, "openai-chat", reply="Sorry, I cannot do that.")
    assert ai.ask("check", want_json=True).data is None


def test_ask_model_reports_a_missing_model_as_a_value_and_accepts_background(client):
    reply = ai.ask_model("hi", background=True, role="background")
    assert reply.ok is False and "No model is selected" in reply.error


def test_ask_passes_images_along_in_the_providers_own_form(client, serve):
    stub, *_ = connect_and_select(client, serve, "anthropic-messages")
    ai.ask("what is this", media=[{"kind": "image", "mimeType": "image/png", "dataBase64": "AAAA"}])
    block = stub.last_body()["messages"][0]["content"][1]
    assert block == {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}


# ================================================================================
# The real turn loop, top to bottom
# ================================================================================

def test_a_real_turn_through_the_orchestrator_is_answered_by_the_selected_provider(client, serve):
    from jarvis import assembly
    from jarvis.orchestrator import TurnRequest
    from jarvis.orchestrator.pipeline import Done, Failed

    stub, _, model_id = connect_and_select(client, serve, "anthropic-messages", model="stub-model-b",
                                           reply="Good morning to you")
    events = list(assembly.get_orchestrator().run_turn(TurnRequest(text="hello there", session_id="s-real")))
    done = [e for e in events if isinstance(e, Done)]
    assert not [e for e in events if isinstance(e, Failed)]
    assert len(done) == 1 and done[0].text == "Good morning to you" and done[0].model_id == "stub-model-b"
    assert wire_model(stub) == "stub-model-b"
    # What Jarvis remembered of it is the provider's reply, attributed to the model that gave it.
    last = conversation.get_messages("s-real")[-1]
    assert last["role"] == "assistant" and last["text"] == "Good morning to you"
    assert last["modelId"] == "stub-model-b" and last["raw"]["adapter"] == "anthropic-messages"


def test_a_real_turn_with_nothing_selected_fails_plainly_and_writes_no_reply(client):
    from jarvis import assembly
    from jarvis.orchestrator import TurnRequest
    from jarvis.orchestrator.pipeline import Failed

    events = list(assembly.get_orchestrator().run_turn(TurnRequest(text="hello there", session_id="s-none")))
    failures = [e for e in events if isinstance(e, Failed)]
    assert len(failures) == 1 and failures[0].code == "no_model"
    assert "No model is selected" in failures[0].error
    assert not [m for m in conversation.get_messages("s-none") if m["role"] == "assistant"]


def test_a_real_turn_that_the_provider_refuses_is_a_failure_and_not_an_answer(client, serve):
    from jarvis import assembly
    from jarvis.orchestrator import TurnRequest
    from jarvis.orchestrator.pipeline import Done, Failed

    connect_and_select(client, serve, "openai-responses", unknown_model="stub-model-a")
    events = list(assembly.get_orchestrator().run_turn(TurnRequest(text="hello there", session_id="s-bad")))
    assert not [e for e in events if isinstance(e, Done)]
    failure = [e for e in events if isinstance(e, Failed)][0]
    assert failure.code == "no_model" and "does not exist" in failure.error
    assert not [m for m in conversation.get_messages("s-bad") if m["role"] == "assistant"]


# ================================================================================
# It survives a restart
# ================================================================================

def test_connections_models_and_the_selection_survive_a_restart(client, serve, scratch):
    from jarvis import assembly, db
    from jarvis.main import create_app

    stub = serve("anthropic-messages", key=SECRET, models=[ANTHROPIC_OPUS, ANTHROPIC_PLAIN])
    connection_id = add(client, stub, key=SECRET)["connection"]["id"]
    select(client, connection_id, "opus-x", "max")
    before = client.get("/api/models").json()

    db.reset_for_tests()          # the database is closed and reopened from disk,
    assembly.reset_for_tests()    # the app is rebuilt from nothing,
    fresh = TestClient(create_app())
    after = fresh.get("/api/models").json()
    assert after == before
    assert after["selection"] == {"auto": False, "providerId": connection_id, "modelId": "opus-x", "effort": "max"}
    assert config.get_secret(f"model_{connection_id}") == SECRET
    # ...and it still works, with the same key and the same model, without being told again.
    events, error = run_step()
    assert error is None and wire_model(stub) == "opus-x"
    assert stub.posts()[-1]["headers"]["x-api-key"] == SECRET
    assert stub.posts()[-1]["body"]["output_config"] == {"effort": "max"}


# ================================================================================
# How Jarvis spends a turn is not which model, and is not effort
# ================================================================================

def test_fast_lowers_the_tool_rounds_and_quality_does_not_change_them(scratch):
    from jarvis import prefs
    from jarvis.orchestrator.pipeline import FAST_MAX_STEPS, MAX_STEPS, step_ceiling

    assert step_ceiling() == MAX_STEPS  # balanced by default
    prefs.set_prefs({"balance": "fast"})
    assert step_ceiling() == FAST_MAX_STEPS < MAX_STEPS
    prefs.set_prefs({"balance": "quality"})
    assert step_ceiling() == MAX_STEPS


def test_the_answer_check_follows_the_balance_and_otherwise_the_standing_preference(scratch):
    from jarvis import prefs
    from jarvis.ops import consequence

    assert consequence.is_enabled() is False
    prefs.set_prefs({"verifyChatAnswers": True})
    assert consequence.is_enabled() is True  # balanced defers to the standing preference
    prefs.set_prefs({"balance": "fast"})
    assert consequence.is_enabled() is False  # fast does not pay for an extra call after the reply
    prefs.set_prefs({"balance": "quality", "verifyChatAnswers": False})
    assert consequence.is_enabled() is True


def test_balance_applies_to_a_model_with_no_effort_control_and_is_stored_apart_from_effort(client, serve):
    stub, connection_id, _ = connect_and_select(client, serve, "openai-chat")
    client.post("/api/prefs", json={"balance": "fast"})
    saved = client.get("/api/prefs").json()
    assert saved["balance"] == "fast" and saved["selectedEffort"] is None  # different keys, different jobs
    from jarvis.orchestrator.pipeline import FAST_MAX_STEPS, step_ceiling

    assert step_ceiling() == FAST_MAX_STEPS
    events, error = run_step()
    assert error is None and "output_config" not in stub.last_body()  # and it sent the model no effort


# ================================================================================
# Small guarantees
# ================================================================================

@pytest.mark.parametrize("typed", ["sk-typed-wrong-key-abcdefghij", "no-well-known-shape-9f8e7d6c5b4a"])
def test_provider_errors_never_carry_the_key_even_when_the_provider_echoes_it(client, serve, typed):
    """The stub echoes back whatever key it was sent, as real providers do. One typed
    key has a recognisable shape; the other has none, so only knowing what was
    saved can scrub it."""
    stub = serve("openai-chat", key=SECRET)
    added = add(client, stub, key=typed)
    assert "Incorrect API key provided" in added["tested"]["message"]  # the provider's own words are kept
    assert typed not in json.dumps(added)
    assert typed not in json.dumps(client.get("/api/models").json())


@pytest.mark.parametrize("format", FORMATS)
def test_a_key_that_stops_working_never_appears_in_the_turns_error(client, serve, format):
    stub, *_ = connect_and_select(client, serve, format, key="no-well-known-shape-1a2b3c4d5e6f")
    stub.key = "some-other-key"
    events, error = run_step()
    assert error is not None and "Incorrect API key provided" in str(error)
    assert "no-well-known-shape-1a2b3c4d5e6f" not in str(error)


def test_the_anthropic_provider_itself_will_not_send_an_effort_it_was_not_told_the_model_accepts(serve):
    """Not only the layers above it: the provider is the last thing between a
    remembered setting and a model that would refuse it."""
    from jarvis.models.providers import anthropic_messages
    from jarvis.models.types import Target

    stub = serve("anthropic-messages")
    target = Target(stub.base_url, "k")
    ask = {"model_id": "m", "messages": [{"role": "user", "text": "hi"}], "system": "", "tools": []}
    list(anthropic_messages.stream(target, effort="low", facts=None, **ask))
    assert "output_config" not in stub.last_body()
    list(anthropic_messages.stream(target, effort="low", facts={"effort": {"levels": ["medium"]}}, **ask))
    assert "output_config" not in stub.last_body()  # a level it was not reported to accept
    list(anthropic_messages.stream(target, effort="low", facts={"effort": {"levels": ["low", "high"]}}, **ask))
    assert stub.last_body()["output_config"] == {"effort": "low"}


def test_the_model_routes_hold_up_when_used_from_many_threads_at_once(live_server, serve):
    """Found by hammering them: every route here reads and writes through ONE shared
    database connection, and the server runs them on a pool of threads. Without the
    store taking turns, the same load returned the wrong rows, raised 'bad parameter
    or other API misuse', and failed saves outright. A real page makes several of
    these requests at once — the picker, the screen and the status check — so this is
    ordinary use, not an artificial one."""
    import threading

    stubs = [serve(f) for f in FORMATS]
    stop = threading.Event()
    failures: list[tuple] = []

    def poll(path: str) -> None:
        with httpx.Client(base_url=live_server, timeout=60) as reader:
            while not stop.is_set():
                reply = reader.get(path)
                if reply.status_code >= 500:
                    failures.append((path, reply.status_code, reply.text[:120]))

    readers = [threading.Thread(target=poll, args=(path,), daemon=True)
               for path in ("/api/models", "/api/status", "/api/models", "/api/prefs")]
    for reader in readers:
        reader.start()
    try:
        with httpx.Client(base_url=live_server, timeout=60) as writer:
            for round_ in range(8):
                stub = stubs[round_ % len(stubs)]
                added = writer.post("/api/models", json={"kind": "custom", "format": stub.format,
                                                         "address": stub.base_url})
                if added.status_code != 200:
                    failures.append(("add", added.status_code, added.text[:120]))
                    continue
                connection = added.json()["connection"]
                if connection["models"]:
                    chosen = writer.post("/api/models/select", json={
                        "providerId": connection["id"], "modelId": connection["models"][0]["id"]})
                    if chosen.status_code != 200:
                        failures.append(("select", chosen.status_code, chosen.text[:120]))
                writer.post(f"/api/models/{connection['id']}/discover")
                writer.delete(f"/api/models/{connection['id']}")
    finally:
        stop.set()
        for reader in readers:
            reader.join(10)
    assert failures == []


def test_a_save_survives_windows_refusing_to_replace_a_file_that_is_being_read(scratch, monkeypatch):
    """`os.replace` fails on Windows with PermissionError if another thread has the
    destination open at that instant — over in moments. A saved setting (a model
    being selected) is not worth failing over it."""
    import os

    from jarvis import store

    real = os.replace
    refusals = {"left": 3}

    def flaky(source, target):
        if refusals["left"]:
            refusals["left"] -= 1
            raise PermissionError(5, "Access is denied")
        return real(source, target)

    monkeypatch.setattr(os, "replace", flaky)
    store.write_json("retry-check", {"saved": True})
    assert store.read_json("retry-check") == {"saved": True}

    # A file that stays locked is still an error, not an endless wait.
    monkeypatch.setattr(os, "replace", lambda *_: (_ for _ in ()).throw(PermissionError(5, "still locked")))
    with pytest.raises(PermissionError):
        store.write_json("retry-check", {"saved": False})


def _arrays_without_items(node, path="", found=None):
    found = [] if found is None else found
    if isinstance(node, dict):
        if node.get("type") == "array" and not node.get("items"):
            found.append(path)
        for key, value in node.items():
            _arrays_without_items(value, f"{path}.{key}", found)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _arrays_without_items(value, f"{path}[{index}]", found)
    return found


def test_every_tool_the_app_really_declares_is_acceptable_to_gemini(scratch):
    """The failure this guards was found live, not by a stub: Google refuses an array
    that does not say what it holds ("items: missing field"), and refuses the WHOLE
    request — so every turn that declared the tool, which was every turn. The app's
    own `check_myself` tool declared two. Found by asking the real API, after eleven
    turns in a row came back unanswered."""
    from jarvis import assembly
    from jarvis.models.providers import gemini_generate

    assembly.reset_for_tests()
    try:
        registry = assembly.get_registry()
        declared = registry.declarations(registry.list())
        assert len(declared) > 20  # the real registry, not an empty one
        # The tools should be right at the source: every argument that is an array says
        # what it holds. (An array INSIDE one — a spreadsheet row's cells — may stay
        # open, because text and numbers both belong there; the translation below
        # covers those.)
        own = [f"{d['name']}.{arg}" for d in declared
               for arg, schema in (d["parameters"].get("properties") or {}).items()
               if isinstance(schema, dict) and schema.get("type") == "array" and not schema.get("items")]
        assert own == [], f"tool arguments that are an array with no element type: {own}"
        # ...and whatever a connector's tool declares, what is SENT is still acceptable.
        sent = [(d["name"], gemini_generate._schema(d["parameters"])) for d in declared]
        assert [n for n, schema in sent if _arrays_without_items(schema)] == []
    finally:
        assembly.reset_for_tests()


@pytest.mark.parametrize("given, expected", [
    ({"type": "array"}, {"type": "array", "items": {"type": "string"}}),
    ({"type": "array", "items": {}}, {"type": "array", "items": {"type": "string"}}),
    # A tool that says what its array holds is never second-guessed.
    ({"type": "array", "items": {"type": "integer"}}, {"type": "array", "items": {"type": "integer"}}),
    # Keywords Gemini refuses are dropped, and a nullable type is put the way Gemini spells it.
    ({"type": ["string", "null"], "additionalProperties": False, "$schema": "x", "default": 1},
     {"type": "string", "nullable": True}),
])
def test_a_tool_schema_is_narrowed_to_what_gemini_accepts(given, expected):
    from jarvis.models.providers import gemini_generate

    assert gemini_generate._schema(given) == expected


def test_the_kinds_the_interface_offers_say_what_each_needs(client):
    body = client.get("/api/models/kinds").json()
    by_id = {k["id"]: k for k in body["kinds"]}
    assert set(by_id) == {"openai", "anthropic", "gemini", "ollama", "lmstudio", "custom"}
    assert by_id["openai"]["address"] == "fixed" and by_id["openai"]["key"] == "required"
    assert by_id["ollama"]["key"] == "none" and by_id["ollama"]["defaultAddress"].endswith(":11434/v1")
    assert by_id["custom"]["chooseFormat"] is True and by_id["custom"]["address"] == "required"
    assert [f["id"] for f in body["formats"]] == list(FORMATS)
    assert not re.search(r"family|capabilit|version|lifecycle|provenance", json.dumps(body), re.IGNORECASE)


# ================================================================================
# Auto — and the promise that a model the person NAMED is never replaced
# ================================================================================

def choose_auto(client):
    reply = client.post("/api/models/select", json={"auto": True})
    assert reply.status_code == 200, reply.text
    return reply.json()


def two_connections(client, serve, *, first: dict | None = None, second: dict | None = None,
                    first_models=None, second_models=None):
    """Two providers with different model ids, connected in that order."""
    one = serve("openai-chat", models=first_models or [{"id": "one-a"}, {"id": "one-b"}], **(first or {}))
    two = serve("openai-chat", models=second_models or [{"id": "two-a"}], **(second or {}))
    first_id = add(client, one, label="One")["connection"]["id"]
    second_id = add(client, two, label="Two")["connection"]["id"]
    return one, two, first_id, second_id


def posted_models(stub: StubProvider) -> list[str]:
    return [r["body"]["model"] for r in stub.posts()]


def test_choosing_auto_is_stored_and_reported_and_naming_a_model_turns_it_off(client, serve):
    stub = serve("openai-chat")
    connection_id = add(client, stub)["connection"]["id"]

    body = choose_auto(client)
    assert body["selection"] == {"auto": True, "providerId": None, "modelId": None, "effort": None}
    assert body["availability"]["state"] == "ok"
    assert client.get("/api/models").json()["selection"]["auto"] is True
    assert client.get("/api/status").json() == {"configured": True}

    body = select(client, connection_id, "stub-model-b").json()
    assert body["selection"]["auto"] is False and body["selection"]["modelId"] == "stub-model-b"
    assert client.get("/api/models").json()["selection"]["auto"] is False


def test_auto_with_nothing_connected_says_so_plainly(client):
    body = choose_auto(client)
    assert body["availability"]["state"] == "none" and "Auto has no model" in body["availability"]["message"]
    assert client.get("/api/status").json() == {"configured": False}
    events, error = run_step()
    assert events == [] and error is not None and "Auto has no model to choose from" in str(error)


def test_auto_answers_from_a_connected_model_and_says_nothing_about_switching(client, serve):
    from jarvis.orchestrator.model_port import ModelSwitched, StepComplete

    stub = serve("openai-chat")
    add(client, stub)
    choose_auto(client)
    events, error = run_step()
    assert error is None and posted_models(stub) == ["stub-model-a"]
    assert not any(isinstance(e, ModelSwitched) for e in events)
    assert isinstance(events[-1], StepComplete) and events[-1].model_id == "stub-model-a"


def test_auto_never_involves_chance(client, serve):
    from jarvis.models import auto

    two_connections(client, serve)
    choose_auto(client)
    orders = {tuple((c.connection.label, c.model.model_id) for c in auto.candidates()) for _ in range(20)}
    assert len(orders) == 1
    assert list(orders)[0] == (("One", "one-a"), ("One", "one-b"), ("Two", "two-a"))


def test_auto_moves_on_when_the_first_model_fails_before_saying_anything_and_says_so(client, serve):
    from jarvis.orchestrator.model_port import ModelSwitched, StepComplete, TextChunk

    one, two, *_ = two_connections(client, serve, first={"unknown_model": "one-a"})
    choose_auto(client)
    events, error = run_step()
    assert error is None
    switched = [e for e in events if isinstance(e, ModelSwitched)]
    assert len(switched) == 1 and switched[0].from_model_id == "one-a" and switched[0].to_model_id == "two-a"
    assert "Auto moved on from one-a on One" in switched[0].reason  # what it left, and why
    # The announcement comes before the words it is about.
    assert events.index(switched[0]) < events.index(next(e for e in events if isinstance(e, TextChunk)))
    assert events[-1].model_id == "two-a" and isinstance(events[-1], StepComplete)
    # It went to the OTHER connection, not to a second model on the one that had just refused.
    assert posted_models(one) == ["one-a"] and posted_models(two) == ["two-a"]


def test_after_a_failure_auto_goes_straight_to_what_worked(client, serve):
    from jarvis.orchestrator.model_port import ModelSwitched

    one, two, *_ = two_connections(client, serve, first={"unknown_model": "one-a"})
    choose_auto(client)
    run_step()
    events, error = run_step()
    assert error is None and not any(isinstance(e, ModelSwitched) for e in events)
    assert posted_models(one) == ["one-a"]  # not asked again
    assert posted_models(two) == ["two-a", "two-a"]


def test_a_named_model_that_fails_is_never_replaced_and_the_error_says_how_to_change_that(client, serve):
    one, two, first_id, _ = two_connections(client, serve, first={"unknown_model": "one-a"})
    assert select(client, first_id, "one-a").status_code == 200
    events, error = run_step()
    assert events == [] and error is not None
    assert "does not exist" in str(error)  # the provider's own words
    assert "Jarvis stays on the model you picked" in str(error) and "Auto" in str(error)
    assert posted_models(one) == ["one-a"] and two.posts() == []
    assert error.detail["reason"] == "provider_error"


def test_auto_does_not_take_over_a_reply_that_has_already_started(client, serve):
    from jarvis.orchestrator.model_port import TextChunk

    one, two, *_ = two_connections(client, serve, first={"truncate": True})
    choose_auto(client)
    events, error = run_step()
    assert any(isinstance(e, TextChunk) for e in events)  # words were already out
    assert error is not None and "stopped part-way" in str(error)
    assert two.posts() == []  # so nobody else finishes the sentence


def test_when_every_model_fails_auto_names_each_one_and_why(client, serve):
    one, two, *_ = two_connections(client, serve, first={"unknown_model": "one-a"},
                                   first_models=[{"id": "one-a"}], second={"unknown_model": "two-a"})
    choose_auto(client)
    events, error = run_step()
    assert events == [] and error is not None
    text = str(error)
    assert "Auto tried 2 models and none could answer" in text and "one-a on One" in text and "two-a on Two" in text
    assert error.detail["reason"] == "auto_exhausted"
    assert [a["model"] for a in error.detail["attempts"]] == ["one-a", "two-a"]


def test_auto_gives_up_after_a_few_models_rather_than_trying_the_whole_catalogue(client, serve):
    from jarvis.models import auto

    dead = [serve("openai-chat", chat_status=400, models=[{"id": f"dead-{i}"}]) for i in range(auto.MAX_ATTEMPTS)]
    fine = serve("openai-chat", models=[{"id": "fine"}])
    for i, stub in enumerate(dead):
        add(client, stub, label=f"Dead{i}")
    add(client, fine, label="Fine")
    choose_auto(client)
    events, error = run_step()
    assert error is not None and f"Auto tried {auto.MAX_ATTEMPTS} models" in str(error)
    assert fine.posts() == []
    # ...and the next message doesn't repeat the mistake: the dead ones now wait behind it.
    events, error = run_step()
    assert error is None and posted_models(fine) == ["fine"]


def test_a_model_the_provider_says_is_not_for_chat_is_never_chosen(client, serve):
    from jarvis.models import auto

    stub = serve("openai-chat", models=[
        {"id": "clip", "type": "video"},
        {"id": "pictures-only", "output_modalities": ["image"]},
        {"id": "no-tools", "capabilities": {"tool_calling": False}},
        {"id": "good", "capabilities": {"tool_calling": True}},
        {"id": "silent"},
    ])
    connection_id = add(client, stub)["connection"]["id"]
    assert store.get_model(connection_id, "clip").facts == {"chat": False}
    assert store.get_model(connection_id, "no-tools").facts == {"tools": False}
    assert store.get_model(connection_id, "silent").facts is None  # it said nothing, so nothing is claimed
    choose_auto(client)
    # Reported tool-capable first; one that said nothing is left in, not guessed at.
    assert [c.model.model_id for c in auto.candidates()] == ["good", "silent"]
    run_step()
    assert posted_models(stub) == ["good"]
    # A person can still name one of the ones Auto passes over — that is their call.
    assert select(client, connection_id, "clip").status_code == 200


def test_a_message_with_a_picture_only_goes_to_a_model_that_did_not_say_it_cannot_see(client, serve):
    from jarvis.models import auto

    stub = serve("openai-chat", models=[{"id": "blind", "input_modalities": ["text"]},
                                        {"id": "sighted", "input_modalities": ["text", "image"]}])
    add(client, stub)
    choose_auto(client)
    assert [c.model.model_id for c in auto.candidates(needs_images=True)] == ["sighted"]
    run_step()
    assert posted_models(stub) == ["blind"]
    run_step(messages=[{"role": "user", "text": "what is this",
                        "media": [{"kind": "image", "mimeType": "image/png", "dataBase64": "AAAA"}]}])
    assert posted_models(stub)[-1] == "sighted"


def test_auto_prefers_what_worked_and_steers_around_what_just_failed_by_how_it_failed(client, serve):
    from datetime import datetime, timedelta, timezone

    from jarvis.models import auto

    stub = serve("openai-chat", models=[{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}])
    cid = add(client, stub)["connection"]["id"]

    def ids(now=None):
        return [c.model.model_id for c in auto.candidates(now=now)]

    assert ids() == ["a", "b", "c", "d"]

    store.record_success(cid, "c")
    assert ids() == ["c", "a", "b", "d"]  # what has worked comes first

    store.record_failure(cid, "a", kind="model", status=404, message="no such model")
    store.record_failure(cid, "b", kind="server", status=503, message="busy")
    now = datetime.now(timezone.utc)
    assert ids(now) == ["c", "d", "a", "b"]  # both just failed: behind the untried one
    assert ids(now + timedelta(minutes=10)) == ["c", "b", "d", "a"]  # a hiccup passes sooner than a refusal
    assert ids(now + timedelta(minutes=40)) == ["c", "a", "b", "d"]  # both forgiven, back in their own order
    assert ids(now) == ids(now)  # the same question gets the same answer

    store.record_failure(cid, "c", kind="rate", status=429, message="slow down")
    assert ids(now) == ["d", "c", "a", "b"]  # a proven model that just failed waits as well — proven first among the waiting
    store.record_success(cid, "c")
    assert ids()[0] == "c"  # and answering again clears it at once


def test_a_pinned_model_is_exact_even_while_auto_is_on(client, serve):
    one, two, *_ = two_connections(client, serve)
    choose_auto(client)
    events, error = run_step(model_id="two-a")
    assert error is None and posted_models(two) == ["two-a"] and one.posts() == []
    events, error = run_step(model_id="not-a-model")
    assert error is not None and "isn't set up on any connection" in str(error)


def test_a_busy_provider_is_asked_again_before_a_named_model_is_given_up_on(client, serve):
    stub, *_ = connect_and_select(client, serve, "openai-chat")
    stub.chat_status = 503
    events, error = run_step()
    assert error is not None and "problem on its end" in str(error)
    assert len(stub.posts()) == 3  # once, and twice more — the same model each time
    assert set(posted_models(stub)) == {"stub-model-a"}


def test_auto_gives_a_busy_model_one_quick_retry_and_then_goes_elsewhere(client, serve):
    one, two, *_ = two_connections(client, serve, first={"chat_status": 503})
    choose_auto(client)
    events, error = run_step()
    assert error is None and len(one.posts()) == 2 and posted_models(two) == ["two-a"]  # one quick retry, then on


def test_what_really_happened_is_recorded_for_auto_to_read(client, serve):
    one, two, first_id, second_id = two_connections(client, serve, first={"unknown_model": "one-a"})
    assert select(client, first_id, "one-a").status_code == 200
    run_step()
    failed = store.list_outcomes()[(first_id, "one-a")]
    assert failed.fail_kind == "model" and failed.fail_status == 404 and failed.last_ok_at is None
    assert select(client, second_id, "two-a").status_code == 200
    run_step()
    assert store.list_outcomes()[(second_id, "two-a")].last_ok_at


def test_a_reply_a_provider_module_cannot_read_is_a_plain_error_and_not_a_crash(client, serve, monkeypatch):
    from types import SimpleNamespace

    from jarvis.models import attempt

    connect_and_select(client, serve, "openai-chat")

    def broken(*args, **kwargs):
        raise KeyError("index")
        yield  # pragma: no cover - makes this a generator, like the real one

    monkeypatch.setattr(attempt.providers, "for_format", lambda _format: SimpleNamespace(stream=broken))
    events, error = run_step()
    assert error is not None and "couldn't read" in str(error)
    assert error.detail["kind"] == "reply"  # said in words, with the same shape as any other failure


def test_auto_gives_a_silent_model_less_time_only_while_another_is_still_waiting(client, serve, monkeypatch):
    from types import SimpleNamespace

    from jarvis.models import attempt, auto
    from jarvis.models.errors import ProviderError

    for i in range(auto.MAX_ATTEMPTS):
        add(client, serve("openai-chat", models=[{"id": f"m{i}"}]), label=f"C{i}")
    choose_auto(client)
    waited: list[float | None] = []

    def refuses(target, **kwargs):
        waited.append(target.read_timeout)
        raise ProviderError("no such model", kind="model", status=404)
        yield  # pragma: no cover

    monkeypatch.setattr(attempt.providers, "for_format", lambda _format: SimpleNamespace(stream=refuses))
    events, error = run_step()
    assert error is not None
    # While others were still waiting a silent model got the short wait; the last one, the generous default.
    assert waited == [auto.FAST_READ_TIMEOUT_S] * (auto.MAX_ATTEMPTS - 1) + [None]


def test_ask_under_auto_moves_on_too_since_nobody_is_watching_it_speak(client, serve):
    one, two, *_ = two_connections(client, serve, first={"unknown_model": "one-a"})
    choose_auto(client)
    answer = ai.ask("What is the answer?")
    assert answer.model_id == "two-a" and posted_models(one) == ["one-a"]


def test_a_plain_server_that_reports_nothing_about_its_models_gets_no_facts_invented(client, serve):
    stub = serve("openai-chat")
    connection_id = add(client, stub)["connection"]["id"]
    assert [m.facts for m in store.list_models(connection_id)] == [None, None]


def test_auto_is_offered_to_voice_and_the_environment_report_like_any_other_choice(client, serve):
    from jarvis.ops.environment import reachability
    from jarvis.voice import options

    add(client, serve("openai-chat"))
    choose_auto(client)
    usable = reachability.models()["usable"]
    assert usable and usable[0]["modelId"] == "stub-model-a"
    assert len(options._ready_models()) == 1


def test_a_gateways_reported_price_is_kept_and_a_402_is_its_own_kind_of_failure(client, serve):
    stub = serve("openai-chat", models=[
        {"id": "paid", "pricing": {"prompt": "0.000002", "completion": "0.000008"}},
        {"id": "free-one", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "unpriced"},
    ])
    cid = add(client, stub)["connection"]["id"]
    assert store.get_model(cid, "paid").facts == {"free": False}
    assert store.get_model(cid, "free-one").facts == {"free": True}
    assert store.get_model(cid, "unpriced").facts is None
    from jarvis.models.providers import _wire

    err = _wire.error_for(402, "Insufficient credits.", "https://x/v1")
    assert err.kind == "billing" and err.status == 402 and "credit" in str(err)


def test_when_the_account_is_out_of_credit_paid_models_wait_and_free_ones_carry_on(client, serve):
    from datetime import datetime, timedelta, timezone

    from jarvis.models import auto

    stub = serve("openai-chat", models=[
        {"id": "a-paid", "pricing": {"prompt": "1", "completion": "1"}},
        {"id": "b-paid", "pricing": {"prompt": "1", "completion": "1"}},
        {"id": "c-free", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "d-unpriced"},
    ])
    cid = add(client, stub)["connection"]["id"]

    def ids(now=None):
        return [c.model.model_id for c in auto.candidates(now=now)]

    assert ids() == ["a-paid", "b-paid", "c-free", "d-unpriced"]
    store.record_failure(cid, "a-paid", kind="billing", status=402, message="Insufficient credits")
    assert ids() == ["c-free", "d-unpriced", "a-paid", "b-paid"]  # b-paid was never tried, and still waits
    later = datetime.now(timezone.utc) + timedelta(minutes=45)
    assert ids(later) == ["a-paid", "b-paid", "c-free", "d-unpriced"]  # credit may have been added by now


def test_auto_starts_from_a_model_that_already_answered_in_the_saved_conversation(client, serve):
    from jarvis.db import get_db
    from jarvis.models import auto

    stub = serve("openai-chat", models=[{"id": "aaa"}, {"id": "zzz-worked"}])
    add(client, stub)
    assert [c.model.model_id for c in auto.candidates()] == ["aaa", "zzz-worked"]
    db = get_db()
    db.execute("INSERT INTO conversations (id, title, created_at, updated_at) VALUES ('c1', 't', 'x', 'x')")
    db.execute("INSERT INTO messages (conversation_id, seq, role, text, payload, created_at) "
               "VALUES ('c1', 1, 'assistant', 'hi', ?, '2026-09-21T11:00:00.000Z')",
               (json.dumps({"modelId": "zzz-worked"}),))
    assert [c.model.model_id for c in auto.candidates()] == ["zzz-worked", "aaa"]


def test_choosing_auto_asks_each_connection_once_more_for_what_it_says_about_its_models(client, serve):
    stub = serve("openai-chat", models=[{"id": "clip", "type": "video"}, {"id": "chat-ok"}])
    cid = add(client, stub)["connection"]["id"]
    # Listed before facts were recorded: as if from an earlier version.
    from jarvis.db import get_db

    get_db().execute("UPDATE provider_models SET facts_json = NULL WHERE provider_id = ?", (cid,))
    assert store.get_model(cid, "clip").facts is None
    choose_auto(client)
    assert store.get_model(cid, "clip").facts == {"chat": False}
    from jarvis.models import auto

    assert [c.model.model_id for c in auto.candidates()] == ["chat-ok"]


def test_a_402_mid_step_skips_the_paid_models_on_that_connection_but_not_the_free_ones(client, serve):
    from jarvis.models import auto

    paid = {"prompt": "1", "completion": "1"}
    free = {"prompt": "0", "completion": "0"}
    stub = serve("openai-chat", chat_status=None, models=[
        {"id": "a-paid", "pricing": paid}, {"id": "b-paid", "pricing": paid},
        {"id": "c-paid", "pricing": paid}, {"id": "d-free", "pricing": free}])
    add(client, stub, label="Gateway")
    choose_auto(client)

    seen = []
    from jarvis.models import attempt
    from jarvis.models.errors import ProviderError
    from jarvis.models.types import Finished
    from types import SimpleNamespace

    def fake(target, **kwargs):
        seen.append(kwargs["model_id"])
        if kwargs["model_id"].endswith("paid"):
            raise ProviderError("Insufficient credits.", kind="billing", status=402)
        yield Finished(text="ok", model_id=kwargs["model_id"])

    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(attempt.providers, "for_format", lambda _f: SimpleNamespace(stream=fake))
        events, error = run_step()
    assert error is None
    assert seen == ["a-paid", "d-free"]  # b-paid and c-paid were never asked: the account has no credit
    assert events[-1].model_id == "d-free"
