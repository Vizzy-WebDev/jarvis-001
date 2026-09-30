"""Moving a conversation between families, two instances of one driver side by side,
and each driver's native encoding of the canonical hints — on real sockets."""

from __future__ import annotations

import json

import pytest

from jarvis import config as secrets
from jarvis import models
from jarvis.models import execute
from jarvis.models.types import (Hints, Message, Requirements, Sealed, Section, TextPart, Tool, ToolCall,
                                 ToolResult)

from layer_helpers import ask, configure, layer  # noqa: F401
from stub_wire import StubWire, Turn

WEATHER = (Tool("get_weather", "Weather in a city.",
                {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}),)
CAPS = {"text_in": True, "tools": True, "parallel_tools": True, "reasoning_control": True, "prompt_caching": True}


@pytest.fixture
def servers(layer, monkeypatch):
    monkeypatch.setattr(execute, "sleep", lambda s: None)
    started = {fmt: StubWire(fmt) for fmt in ("anthropic_messages", "gemini_generate", "openai_chat",
                                              "openai_responses")}
    for stub in started.values():
        stub.start()
    names = {"anthropic_messages": "anthropic", "gemini_generate": "gemini", "openai_chat": "openai",
             "openai_responses": "responses"}
    connections = [{"name": names[fmt], "driver": fmt, "base_url": stub.base_url, "trust": "standard",
                    "models": {"m": {"capabilities": CAPS, "family": {"anthropic_messages": "claude"}.get(fmt, names[fmt])}},
                    **({"quirks": "gemini"} if fmt == "gemini_generate" else {})}
                   for fmt, stub in started.items()]
    configure(layer, connections, settings={"retries": 0},
              aliases={name: {"endpoint": f"{name}/m"} for name in ("anthropic", "gemini", "openai")})
    yield started
    for stub in started.values():
        stub.stop()


def on(request, alias):
    return type(request)(**{**request.__dict__, "requirements": Requirements(pin=alias)})


def test_a_tool_conversation_moves_across_families(servers):
    anthropic, gemini, chat = (servers["anthropic_messages"], servers["gemini_generate"], servers["openai_chat"])
    anthropic.queue(Turn(tools=[("get_weather", {"city": "Paris"})]))
    base = ask("Weather in Paris?", tools=WEATHER)
    first = models.generate(on(base, "anthropic"))
    call = first.tool_calls[0]
    assert any(isinstance(i, Sealed) and i.kind == "thinking" for i in first.items)

    history = (*base.items, *first.items, ToolResult(call.id, call.name, {"temp": 21}))
    gemini.queue(Turn(text="It's 21 degrees in Paris."))
    second = models.generate(on(type(base)(**{**base.__dict__, "items": history}), "gemini"))
    assert second.text == "It's 21 degrees in Paris."
    assert second.report.features["foreign_provider_state"] == "dropped"
    body = gemini.last_body()
    text = json.dumps(body)
    assert "SIG-abc123" not in text and "toolu_0" not in text  # Anthropic's own state and ids stay with Anthropic
    model_parts = [p for c in body["contents"] if c["role"] == "model" for p in c["parts"]]
    assert model_parts[0]["functionCall"]["id"] == call.id
    assert model_parts[0]["thoughtSignature"] == "skip_thought_signature_validator"
    response_part = [p for c in body["contents"] if c["role"] == "user" for p in c["parts"] if "functionResponse" in p][0]
    assert response_part["functionResponse"]["id"] == call.id

    # And on to a third family, carrying everything so far.
    history2 = (*history, *second.items, Message("user", (TextPart("Thanks!"),)))
    chat.queue(Turn(text="You're welcome."))
    third = models.generate(on(type(base)(**{**base.__dict__, "items": history2}), "openai"))
    assert third.text == "You're welcome."
    sent = chat.last_body()["messages"]
    assert [c["id"] for m in sent if m["role"] == "assistant" for c in m.get("tool_calls") or []] == [call.id]
    assert [m["tool_call_id"] for m in sent if m["role"] == "tool"] == [call.id]
    assert "GEMSIG" not in json.dumps(sent) and "SIG-abc123" not in json.dumps(sent)

    # Back to Anthropic: its own thinking and its own tool id come back, exactly.
    anthropic.queue(Turn(text="Anything else?"))
    models.generate(on(type(base)(**{**base.__dict__, "items": history2}), "anthropic"))
    body = anthropic.last_body()
    assistant = [b for m in body["messages"] if m["role"] == "assistant" for b in m["content"]]
    assert assistant[0] == {"type": "thinking", "thinking": "Let me think.", "signature": "SIG-abc123"}
    assert [b["id"] for b in assistant if b["type"] == "tool_use"] == ["toolu_0"]


def test_two_instances_of_one_driver_with_overlapping_model_ids(layer, monkeypatch):
    monkeypatch.setattr(execute, "sleep", lambda s: None)
    desk = StubWire("openai_chat", models=[{"id": "llama3"}, {"id": "qwen"}])
    laptop = StubWire("openai_chat", models=[{"id": "llama3"}])
    for stub in (desk, laptop):
        stub.start()
    try:
        configure(layer, [{"name": "desk", "driver": "openai_chat", "base_url": desk.base_url, "trust": "local",
                           "quirks": "ollama"},
                          {"name": "laptop", "driver": "openai_chat", "base_url": laptop.base_url, "trust": "local",
                           "quirks": "ollama"}],
                  aliases={"laptop-llama": {"endpoint": "laptop/llama3"}, "desk-llama": {"endpoint": "desk/llama3"}},
                  settings={"retries": 0})
        models.refresh_catalog()
        assert {e.id for e in models.list_endpoints()} == {"desk/llama3", "desk/qwen", "laptop/llama3"}
        laptop.queue(Turn(text="from the laptop"))
        response = models.generate(ask(requirements=Requirements(pin="laptop-llama")))
        assert response.text == "from the laptop" and not desk.generations()
        desk.stop()  # the desk goes away: its llama3 is not the laptop's llama3, and nothing mixes them up
        laptop.queue(Turn(text="still the laptop"))
        moved = models.generate(ask(prefer=("desk-llama",)))
        assert moved.provenance.endpoint_id == "laptop/llama3" and moved.text == "still the laptop"
        # The desk (asked for first) fails; the fallback goes to the other upstream, not the desk's other model.
        assert [f.to_endpoint for f in moved.provenance.fallbacks] == ["laptop/llama3"]
    finally:
        desk.stop()
        laptop.stop()


def test_each_driver_encodes_the_canonical_hints_natively(servers):
    request = ask(instructions=(Section("persona", "Be brief."), Section("memories", "Likes tea."),
                                Section("now", "It is noon.")),
                  hints=Hints(stable_prefix_until="memories", reasoning_effort="high", max_output_tokens=300))
    for alias, fmt in (("anthropic", "anthropic_messages"), ("gemini", "gemini_generate"),
                       ("openai", "openai_chat")):
        response = models.generate(on(request, alias))
        assert response.report.features["reasoning_effort"] == "native"
    a = servers["anthropic_messages"].last_body()
    assert a["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "<persona>" in a["system"][0]["text"] and "<memories>" in a["system"][0]["text"]  # the claude profile
    assert "cache_control" not in a["system"][1] and "It is noon." in a["system"][1]["text"]
    assert a["output_config"] == {"effort": "high"} and a["max_tokens"] == 300
    g = servers["gemini_generate"].last_body()
    assert g["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "high"}
    assert g["generationConfig"]["maxOutputTokens"] == 300
    assert g["systemInstruction"]["parts"][0]["text"].startswith("## Persona")
    c = servers["openai_chat"].last_body()
    assert c["reasoning_effort"] == "high" and c["max_tokens"] == 300


def test_responses_is_stateless_and_asks_for_encrypted_reasoning(layer, monkeypatch):
    monkeypatch.setattr(execute, "sleep", lambda s: None)
    stub = StubWire("openai_responses")
    stub.start()
    try:
        configure(layer, [{"name": "oa", "driver": "openai_responses", "base_url": stub.base_url,
                           "trust": "standard", "models": {"m": {"capabilities": CAPS}}}], settings={"retries": 0})
        models.generate(ask(hints=Hints(reasoning_effort="low")))
        body = stub.last_body()
        assert body["store"] is False and body["include"] == ["reasoning.encrypted_content"]
        assert body["reasoning"] == {"effort": "low"}
    finally:
        stub.stop()


def test_gemini_refuses_an_array_without_items_by_being_ineligible(servers):
    loose = (Tool("tag", "", {"type": "object", "properties": {"tags": {"type": "array"}}}),)
    explanation = models.explain_route(ask(tools=loose))
    rejected = {r.endpoint_id: r.reason for r in explanation.rejected}
    assert rejected == {"gemini/m": "schema_not_expressible"}
    assert "gemini/m" not in explanation.ranked and "anthropic/m" in explanation.ranked
    assert "responses/m" in explanation.ranked
