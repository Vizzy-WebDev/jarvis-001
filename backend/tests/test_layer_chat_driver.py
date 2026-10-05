"""The Chat Completions driver's quirk profiles, its discovery (a rich gateway
listing, Ollama's /api/show), embeddings, and probing — on a real socket."""

from __future__ import annotations

import pytest

from jarvis import models
from jarvis.models import capabilities, config, execute, probe, state
from jarvis.models.types import Hints, OutputSpec, Section, Tool

from layer_helpers import ask, configure, layer  # noqa: F401
from stub_wire import StubWire, Turn


@pytest.fixture
def chat(layer, monkeypatch):
    monkeypatch.setattr(execute, "sleep", lambda s: None)
    stub = StubWire("openai_chat")
    stub.start()
    yield stub
    stub.stop()


def connect(layer, stub, *, quirks=None, models_=None, **extra):
    entry = {"name": "box", "driver": "openai_chat", "base_url": stub.base_url, "trust": "local",
             "models": models_ if models_ is not None else {"stub-a": {"capabilities": {"reasoning_control": True,
                                                                                         "json_mode": True}}},
             **extra}
    if quirks:
        entry["quirks"] = "custom"
    return configure(layer, [entry], quirk_profiles={"custom": {"wire": quirks}} if quirks else {},
                     settings={"retries": 0})


INSTRUCTED = ask(instructions=(Section("persona", "Be brief."), Section("task", "Answer.")))


def test_plain_server_defaults(chat, layer):
    connect(layer, chat)
    models.generate(type(INSTRUCTED)(**{**INSTRUCTED.__dict__, "tools": (Tool("t", "", {"type": "object"}),),
                                        "hints": Hints(max_output_tokens=50, reasoning_effort="low")}))
    body = chat.last_body()
    assert body["messages"][0] == {"role": "system", "content": "## Persona\n\nBe brief.\n\n## Task\n\nAnswer."}
    assert body["stream_options"] == {"include_usage": True}
    assert body["parallel_tool_calls"] is True
    assert body["max_tokens"] == 50 and body["reasoning_effort"] == "low"


def test_quirks_change_only_the_wire(chat, layer):
    connect(layer, chat, quirks={"no_system_role": True, "no_stream_usage": True, "no_parallel_tool_calls": True,
                                 "max_tokens_param": "max_completion_tokens", "reasoning_param": "reasoning_object"})
    models.generate(type(INSTRUCTED)(**{**INSTRUCTED.__dict__, "tools": (Tool("t", "", {"type": "object"}),),
                                        "hints": Hints(max_output_tokens=50, reasoning_effort="high")}))
    body = chat.last_body()
    assert [m["role"] for m in body["messages"]] == ["user"]
    assert body["messages"][0]["content"].startswith("## Persona\n\nBe brief.") and body["messages"][0][
        "content"].endswith("hello")
    assert "stream_options" not in body and "parallel_tool_calls" not in body
    assert body["max_completion_tokens"] == 50 and body["reasoning"] == {"effort": "high"}


def test_rejects_strict_sends_json_mode_instead(chat, layer):
    connect(layer, chat, quirks={"rejects_strict": True},
            models_={"stub-a": {"capabilities": {"structured_output_strict": True}}})
    chat.queue(Turn(text='{"ok": true}'))
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    assert models.generate(ask(output=OutputSpec("json", schema))).data == {"ok": True}
    assert chat.last_body()["response_format"] == {"type": "json_object"}


def test_tool_args_not_streamed_arrive_whole(chat, layer):
    connect(layer, chat, quirks={"tool_args_not_streamed": True},
            models_={"stub-a": {"capabilities": {"tools": True}}})
    chat.queue(Turn(tools=[("t", {"a": 1, "b": "two"})]))
    events = list(models.stream(ask(tools=(Tool("t", "", {"type": "object"}),))))
    deltas = [e for e in events if type(e).__name__ == "ToolArgsDelta"]
    assert len(deltas) == 1 and deltas[0].delta == '{"a": 1, "b": "two"}'


def test_default_params_reach_the_wire_and_extensions_only_their_driver(chat, layer):
    connect(layer, chat, default_params={"provider": {"allow_fallbacks": False}})
    response = models.generate(ask(extensions={"openai_chat": {"provider": {"order": ["x"]}, "top_k": 3},
                                               "anthropic_messages": {"top_k": 9}}))
    body = chat.last_body()
    assert body["provider"] == {"allow_fallbacks": False, "order": ["x"]} and body["top_k"] == 3
    assert response.report.features["extensions.anthropic_messages"] == "dropped"
    assert any("anthropic_messages" in w for w in response.report.warnings)


def test_a_rich_gateway_listing_becomes_discovered_capabilities_and_prices(layer):
    stub = StubWire("openai_chat", models=[
        {"id": "vendor/smart", "name": "Smart", "context_length": 200000,
         "supported_parameters": ["tools", "structured_outputs", "response_format", "reasoning"],
         "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
         "pricing": {"prompt": "0.000003", "completion": "0.000015"},
         "top_provider": {"max_completion_tokens": 64000}},
        {"id": "vendor/free:free", "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": ["tools"]},
        {"id": "router/auto", "pricing": {"prompt": "-1", "completion": "-1"}},
        {"id": "vendor/painter", "architecture": {"output_modalities": ["image"]}},
    ])
    stub.start()
    try:
        configure(layer, [{"name": "gw", "driver": "openai_chat", "base_url": stub.base_url, "trust": "standard",
                           "quirks": "openrouter"}])
        assert models.refresh_catalog()["gw"]["ok"]
        by_id = {e.id: e for e in models.list_endpoints()}
        smart = by_id["gw/vendor/smart"]
        caps = capabilities.plain(smart.capabilities)
        assert caps["image_in"] and caps["structured_output_strict"] and caps["json_mode"] and caps["reasoning_control"]
        assert caps["max_context_tokens"] == 200000 and caps["max_output_tokens"] == 64000
        assert smart.capabilities["tools"].source == "discovered"
        assert smart.pricing.input == pytest.approx(3.0) and smart.pricing.output == pytest.approx(15.0)
        assert by_id["gw/vendor/free:free"].pricing.free
        assert by_id["gw/router/auto"].pricing is None  # a router's price depends on who answers: unknown
        assert capabilities.plain(by_id["gw/vendor/painter"].capabilities)["text_in"] is False
    finally:
        stub.stop()


def test_ollama_show_fills_in_what_the_plain_list_cannot(layer):
    stub = StubWire("openai_chat", models=[{"id": "llama3.1:8b"}, {"id": "llava"}, {"id": "nomic-embed"}],
                    show={"llama3.1:8b": {"capabilities": ["completion", "tools"], "details": {"family": "llama"},
                                          "model_info": {"llama.context_length": 131072}},
                          "llava": {"capabilities": ["completion", "vision"], "details": {"family": "llama"}},
                          "nomic-embed": {"capabilities": ["embedding"], "details": {"family": "nomic-bert"}}})
    stub.start()
    try:
        configure(layer, [{"name": "desk", "driver": "openai_chat", "base_url": stub.base_url, "trust": "local",
                           "quirks": "ollama"}])
        models.refresh_catalog()
        by_id = {e.id: e for e in models.list_endpoints()}
        llama = capabilities.plain(by_id["desk/llama3.1:8b"].capabilities)
        assert llama["tools"] and not llama["image_in"] and llama["max_context_tokens"] == 131072
        assert llama["parallel_tools"] is False  # the ollama profile's declared default
        assert by_id["desk/llama3.1:8b"].family == "llama" and by_id["desk/llama3.1:8b"].pricing.free
        assert capabilities.plain(by_id["desk/llava"].capabilities)["image_in"]
        embed = capabilities.plain(by_id["desk/nomic-embed"].capabilities)
        assert embed["embeddings"] and embed["text_in"] is False
        assert [r["body"]["model"] for r in stub.requests if r["kind"] == "show"] == ["llama3.1:8b", "llava",
                                                                                       "nomic-embed"]
    finally:
        stub.stop()


def test_probing_writes_state_and_asks_before_a_paid_endpoint(chat, layer):
    connect(layer, chat, trust="standard", models_={"stub-a": {"pricing": {"input": 1, "output": 1}}})
    asked: list[str] = []
    result = probe.probe("box/stub-a", cases=["streaming"], confirm=lambda q: asked.append(q) or False)
    assert not result["ran"] and "costing about $" in asked[0]
    assert "box/stub-a" not in state.probed()

    chat.queue(Turn(text="ready"), Turn(tools=[("get_number", {})]), Turn(text="It was 42."))
    result = probe.probe("box/stub-a", cases=["streaming", "tool_roundtrip"], confirm=True)
    assert result["capabilities"] == {"streaming": True, "tools": True}
    assert state.probed()["box/stub-a"] == {"streaming": True, "tools": True}
    # Written to state, never to config.
    assert "probe" not in (layer.data_dir / "models.yaml").read_text(encoding="utf-8")
    endpoint = {e.id: e for e in models.list_endpoints()}["box/stub-a"]
    assert endpoint.capabilities["tools"].source == "probed"


def test_a_free_endpoint_is_probed_without_asking(chat, layer):
    connect(layer, chat)  # local: priced at zero
    chat.queue(Turn(text="ready"))
    result = probe.probe("box/stub-a", cases=["streaming"], confirm=lambda q: pytest.fail("asked"))
    assert result["ran"] and result["capabilities"] == {"streaming": True}


def test_config_is_never_touched_by_discovery(chat, layer):
    connect(layer, chat)
    before = (layer.data_dir / "models.yaml").read_text(encoding="utf-8")
    models.refresh_catalog()
    assert (layer.data_dir / "models.yaml").read_text(encoding="utf-8") == before
    assert config.current().connections["box"].models.keys() == {"stub-a"}
