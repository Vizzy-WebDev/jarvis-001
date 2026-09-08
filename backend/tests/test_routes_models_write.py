"""Adding a connection, and finding out whether it works.

The behaviour worth pinning is not CRUD — it is the judgment around it: what
gets tested before a connection is saved, what a failure says, what never leaves
the process, and the rate limit that exists because the unbounded version of
this mass-banned a real roster.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from jarvis.gateway import availability, connections, registry, setup

from stub_openai_server import StubModelServer


@pytest.fixture
def client(scratch):
    from jarvis.main import create_app

    availability.reset_for_tests()
    yield TestClient(create_app())
    availability.reset_for_tests()


@pytest.fixture
def stub():
    server = StubModelServer()
    server.base_url = server.start()
    yield server
    server.stop()


def test_adding_one_model_proves_that_model_can_answer(client, stub):
    """One model is added, so the specific question is asked: can THIS model
    produce a token. Listing would only prove the address exists."""
    stub.says("ready")
    added = client.post("/api/connections", json={
        "adapter": "openai-compatible", "baseUrl": stub.base_url,
        "label": "stub", "models": ["stub-model"]}).json()

    assert added["ok"] is True
    assert [m["model"] for m in added["added"]] == ["stub-model"]
    assert stub.requests, "nothing was actually asked of the model"
    assert stub.requests[-1]["path"].endswith("/chat/completions")


def test_adding_several_models_proves_the_ADDRESS_not_one_arbitrary_model(client, stub):
    """The original rejected a real, working connection over exactly one bad
    route among 115 good ones, because it validated a multi-model add by
    generating with whichever model happened to sort first. Several models means
    the honest question is "is this connection real", answered by listing."""
    added = client.post("/api/connections", json={
        "adapter": "openai-compatible", "baseUrl": stub.base_url,
        "label": "stub", "models": ["one", "two", "three"]}).json()

    assert added["ok"] is True
    assert len(added["added"]) == 3
    assert all(not r["path"].endswith("/chat/completions") for r in stub.requests), \
        "a multi-model add generated with one arbitrary model"


def test_a_connection_that_cannot_be_reached_is_refused_with_a_reason(client):
    refused = client.post("/api/connections", json={
        "adapter": "openai-compatible", "baseUrl": "http://127.0.0.1:19999/v1",
        "label": "nothing there", "models": ["a", "b"]})
    assert refused.status_code == 400
    assert refused.json()["error"]
    assert connections.list_connections() == []


def test_no_provider_and_no_adapter_is_a_plain_refusal(client):
    answer = client.post("/api/connections", json={"models": ["x"]})
    assert answer.status_code == 400
    assert "provider" in answer.json()["error"].lower()


def test_a_key_goes_in_and_never_comes_back_out(client, stub):
    stub.says("ready")
    added = client.post("/api/connections", json={
        "adapter": "openai-compatible", "baseUrl": stub.base_url, "label": "keyed",
        "secret": "sk-super-secret-value", "models": ["stub-model"]})
    assert added.status_code == 200
    assert "sk-super-secret-value" not in added.text
    assert "sk-super-secret-value" not in client.get("/api/models").text
    # It really was stored — the connection can say a key is set.
    assert client.get("/api/models").json()["connections"][0]["hasSecret"] is True


def test_discovery_distinguishes_found_nothing_from_could_not_reach(client, stub):
    stub.models = [{"id": "alpha"}, {"id": "beta"}]
    found = client.post("/api/connections/discover", json={
        "adapter": "openai-compatible", "baseUrl": stub.base_url}).json()
    assert [m["model"] for m in found["models"]] == ["alpha", "beta"]
    assert found["error"] is None

    unreachable = client.post("/api/connections/discover", json={
        "adapter": "openai-compatible", "baseUrl": "http://127.0.0.1:19999/v1"}).json()
    assert unreachable["models"] == []
    assert unreachable["error"], "an unreachable address answered like an empty one"


def test_discovery_against_a_saved_connection_hides_what_is_already_added(client, stub):
    """(connection, model) is the uniqueness rule, so offering a model that is
    already there just to have it refused is noise. Scoped to THAT connection —
    the same model under another one still appears."""
    stub.says("ready")
    stub.models = [{"id": "alpha"}, {"id": "beta"}]
    conn_id = client.post("/api/connections", json={
        "adapter": "openai-compatible", "baseUrl": stub.base_url, "label": "stub",
        "models": ["alpha"]}).json()["connection"]["id"]

    found = client.post("/api/connections/discover", json={"connectionId": conn_id}).json()
    assert [m["model"] for m in found["models"]] == ["beta"]


def test_removing_a_connection_removes_the_models_that_hung_off_it(client, stub):
    stub.says("ready")
    conn_id = client.post("/api/connections", json={
        "adapter": "openai-compatible", "baseUrl": stub.base_url, "label": "stub",
        "models": ["one", "two"]}).json()["connection"]["id"]
    # Two models: the multi-add path, so nothing was generated with.
    assert len(registry.list_models()) == 2

    answer = client.delete(f"/api/connections/{conn_id}").json()
    assert answer == {"ok": True, "removedModels": 2}
    assert registry.list_models() == []


def test_testing_a_model_updates_its_badge_both_ways(client, stub):
    stub.says("ready")
    model_id = client.post("/api/connections", json={
        "adapter": "openai-compatible", "baseUrl": stub.base_url, "label": "stub",
        "models": ["stub-model"]}).json()["added"][0]["id"]

    stub.fails(500, "everything is on fire")
    assert client.post(f"/api/models/{model_id}/test").json()["ok"] is False
    assert client.get("/api/models").json()["health"][model_id]["reason"]

    stub.says("ready")
    assert client.post(f"/api/models/{model_id}/test").json()["ok"] is True
    assert client.get("/api/models").json()["health"] == {}


def test_a_model_that_never_existed_is_a_clean_404(client):
    assert client.post("/api/models/nope/test").status_code == 404
    assert client.patch("/api/models/nope", json={"label": "x"}).status_code == 404


def test_the_default_recheck_only_touches_what_is_not_already_working(client, stub):
    """A model already answering needs no proof, and proving it again costs a
    real request on a roster that is routinely rate-limited."""
    stub.says("ready")
    client.post("/api/connections", json={
        "adapter": "openai-compatible", "baseUrl": stub.base_url, "label": "stub",
        "models": ["good"]})
    before = len(stub.requests)

    client.post("/api/models/recheck", json={})
    assert len(stub.requests) == before, "a working model was re-tested for nothing"

    client.post("/api/models/recheck", json={"scope": "all"})
    assert len(stub.requests) > before


def test_the_preview_costs_nothing_to_ask(client, stub):
    stub.says("ready")
    client.post("/api/connections", json={
        "adapter": "openai-compatible", "baseUrl": stub.base_url, "label": "stub",
        "models": ["good"]})
    before = len(stub.requests)

    preview = client.get("/api/models/recheck/preview").json()
    assert preview["total"] == 1
    assert preview["byConnection"][0]["count"] == 1
    assert len(stub.requests) == before, "the preview made a real model call"


def test_probing_an_unknown_address_reports_what_it_tried(client, stub):
    """The failure this whole flow was written for: one generic sentence with no
    way to tell what went wrong."""
    probed = client.post("/api/connections/probe", json={"baseUrl": stub.base_url}).json()
    assert probed["steps"], "a probe that explains nothing is the original bug"

    nothing = client.post("/api/connections/probe",
                          json={"baseUrl": "http://127.0.0.1:19999"}).json()
    assert nothing["ok"] is False and nothing["steps"]
