"""Embedding spaces (never a different model; declared backups; policy) and the
trace record every call leaves, with spend queries over them."""

from __future__ import annotations

import pytest

from jarvis import models
from jarvis.models import errors, execute, state, trace
from jarvis.models.drivers import fake
from jarvis.models.types import OutputSpec, Tool

from layer_helpers import ask, configure, fake_conn, layer  # noqa: F401
from stub_wire import StubWire


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(execute, "sleep", lambda s: None)


@pytest.fixture
def traced(layer):
    from jarvis.db import get_db

    db = get_db()
    for statement in [s for s in trace.TABLE_SQL.split(";") if s.strip()]:
        db.execute(statement)
    return layer


def space_config(layer, **space):
    return configure(layer, [fake_conn("main"), fake_conn("mirror"), fake_conn("other"),
                             fake_conn("cloud", trust="standard")],
                     embedding_spaces={"notes": {"primary": "main/emb", "backups": ["mirror/emb"], "dimension": 2,
                                                 "model_version": "emb-v1", **space}})


# --- embeddings --------------------------------------------------------------------------------

def test_embed_returns_vectors_with_the_space_and_model_version(layer):
    space_config(layer)
    result = models.embed("notes", ["hello", "hi"], data_class="personal")
    assert result.vectors == ((5.0, 0.0), (2.0, 0.0))
    assert result.space == "notes" and result.model_version == "emb-v1" and result.endpoint_id == "main/emb"


def test_a_declared_backup_is_used_when_the_primary_is_down(layer):
    space_config(layer)
    fake.queue("main", errors.Unavailable("down"))
    assert models.embed("notes", ["x"], data_class="personal").endpoint_id == "mirror/emb"


def test_nothing_outside_the_space_ever_stands_in(layer):
    space_config(layer)
    fake.queue("main", errors.Unavailable("down"))
    fake.queue("mirror", errors.Timeout("slow"))
    with pytest.raises(errors.Unavailable, match="nothing else may stand in"):
        models.embed("notes", ["x"], data_class="personal")
    assert fake.calls("other") == []


def test_a_vector_of_the_wrong_size_is_refused_not_used(layer):
    space_config(layer, dimension=3)
    with pytest.raises(errors.InvalidRequest, match="isn't the model this space was made with"):
        models.embed("notes", ["x"], data_class="personal")


def test_data_policy_applies_to_embeddings(layer):
    configure(layer, [fake_conn("cloud", trust="standard")],
              embedding_spaces={"s": {"primary": "cloud/emb", "dimension": 2}},
              policies={"data_classes": {"sensitive": ["local"]}})
    with pytest.raises(errors.NoEligibleEndpoint, match="privacy settings"):
        models.embed("s", ["x"], data_class="sensitive")
    assert models.embed("s", ["x"], data_class="personal").endpoint_id == "cloud/emb"


def test_embeddings_over_the_chat_completions_wire(layer):
    stub = StubWire("openai_chat")
    stub.start()
    try:
        configure(layer, [{"name": "box", "driver": "openai_chat", "base_url": stub.base_url, "trust": "local"}],
                  embedding_spaces={"s": {"primary": "box/nomic", "dimension": 3}})
        result = models.embed("s", ["alpha", "be"], data_class="personal")
        assert result.vectors == ((5.0, 0.0, 0.5), (2.0, 1.0, 0.5))  # put back in input order
        assert [r["body"] for r in stub.requests if r["kind"] == "embed"] == [{"model": "nomic",
                                                                               "input": ["alpha", "be"]}]
    finally:
        stub.stop()


# --- traces ------------------------------------------------------------------------------------

def test_every_call_leaves_a_metadata_trace(traced):
    configure(traced, [fake_conn("a", models={"m": {"pricing": {"input": 1, "output": 1},
                                                   "capabilities": {"text_in": True, "tools": True}}}), fake_conn("b"),
                       fake_conn("cloud", trust="standard")],
              settings={"retries": 0}, policies={"data_classes": {"sensitive": ["local"]}})
    fake.queue("a", errors.Unavailable("a down"))
    fake.queue("b", fake.tools(("t", "{broken")))
    response = models.generate(ask("secret words", data_class="sensitive", affinity_key="s1",
                                   tools=(Tool("t", "", {"type": "object"}),)))
    row = trace.recent(1)[0]
    assert row["request_id"] == response.request_id
    assert (row["task_class"], row["data_class"], row["affinity_key"]) == ("chat", "sensitive", "s1")
    assert row["outcome"] == "ok" and row["endpoint_id"] == "b/m" and row["connection"] == "b"
    assert row["detail"]["ranked"] == ["a/m", "b/m"]
    assert row["detail"]["rejected"][0]["endpoint"] == "cloud/m"
    assert [a["error_type"] for a in row["detail"]["attempts"]] == ["unavailable", None]
    assert row["detail"]["fallbacks"][0]["to_endpoint"] == "b/m"
    assert row["detail"]["signals"] == {"invalid_tool_arguments": 1, "fallbacks": 1}
    assert row["detail"]["report"]["features"]["tools"] == "native"
    assert row["total_ms"] is not None and row["input_tokens"] == 10
    assert row["content"] is None  # metadata only by default
    assert "secret words" not in str(row)


def test_a_failed_call_is_traced_too(traced):
    configure(traced, [fake_conn("cloud", trust="standard")], policies={"data_classes": {"sensitive": ["local"]}})
    with pytest.raises(errors.NoEligibleEndpoint):
        models.generate(ask(data_class="sensitive"))
    row = trace.recent(1)[0]
    assert row["outcome"] == "no_eligible_endpoint" and row["detail"]["ranked"] == []


def test_content_is_kept_only_when_switched_on(traced):
    configure(traced, [fake_conn("a")], settings={"trace_content": True})
    models.generate(ask("keep this"))
    content = trace.recent(1)[0]["content"]
    assert "keep this" in str(content["request"]) and content["response"]["items"]


def test_schema_repair_is_a_signal(traced):
    configure(traced, [fake_conn("a", models={"m": {"capabilities": {"text_in": True}}})])
    fake.queue("a", fake.reply("no"), fake.reply('{"ok": true}'))
    models.generate(ask(output=OutputSpec("json", {"type": "object", "required": ["ok"]}, "best_effort")))
    assert trace.recent(1)[0]["detail"]["signals"]["schema_repair_used"] == 1


def test_spend_by_connection_task_class_and_day(traced):
    configure(traced, [fake_conn("a", models={"m": {"pricing": {"input": 1000, "output": 0}}}),
                       fake_conn("b", trust="standard")], aliases={"b": {"endpoint": "b/m"}})
    from jarvis.models.types import Requirements

    models.generate(ask(task_class="chat"))
    models.generate(ask(task_class="memory_review"))
    models.generate(ask(task_class="chat", requirements=Requirements(pin="b")))
    by_conn = {r["key"]: r for r in trace.spend("connection")}
    assert by_conn["a"]["cost"] == pytest.approx(0.02) and by_conn["a"]["calls"] == 2
    assert by_conn["b"]["unpriced_calls"] == 1 and by_conn["b"]["cost"] == 0
    by_task = {r["key"]: r["calls"] for r in trace.spend("task_class")}
    assert by_task == {"chat": 2, "memory_review": 1}
    [today] = trace.spend("day")
    assert today["calls"] == 3
    assert state.month_spend() == pytest.approx(0.02)
