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
def client(scratch):
    from jarvis import assembly
    from jarvis.main import create_app

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
    assert after["selection"] == {"providerId": connection_id, "modelId": "opus-x", "effort": "max"}
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
