"""The build's definition of done, walked end to end on real sockets:

OpenAI, Anthropic, OpenRouter (its own fallback off through default_params) and two
separate Ollama instances connected at once by editing config only; a "tools,
sensitive data, optimize cost" request routed to an allowed local endpoint with
every other rejection explained; that endpoint going offline and the request
falling back to the other allowed one, visibly, or failing when there is none;
strict output on a JSON-mode-only model failing with `required` and coming back
emulated with `best_effort`; and a mid-conversation switch.
"""

from __future__ import annotations

import json

import pytest
import yaml

from jarvis import config as secrets
from jarvis import models
from jarvis.models import config, errors, execute
from jarvis.models.types import OutputSpec, Requirements, Sealed, Tool, ToolResult

from layer_helpers import ask, layer  # noqa: F401
from stub_wire import StubWire, Turn

TOOLS = (Tool("lookup", "Look something up.",
              {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}),)
SHOW = {"llama3.1:8b": {"capabilities": ["completion", "tools"], "details": {"family": "llama"},
                        "model_info": {"llama.context_length": 131072}}}


@pytest.fixture
def world(layer, monkeypatch):
    monkeypatch.setattr(execute, "sleep", lambda s: None)
    stubs = {
        "openai": StubWire("openai_responses", key="sk-oa", models=[{"id": "gpt-x"}]),
        "anthropic": StubWire("anthropic_messages", key="sk-an", models=[{"id": "claude-x"}]),
        "openrouter": StubWire("openai_chat", key="sk-or", models=[
            {"id": "vendor/json-only", "supported_parameters": ["tools", "response_format"],
             "pricing": {"prompt": "0.000001", "completion": "0.000002"}}]),
        "ollama-desk": StubWire("openai_chat", models=[{"id": "llama3.1:8b"}], show=SHOW),
        "ollama-laptop": StubWire("openai_chat", models=[{"id": "llama3.1:8b"}], show=SHOW),
    }
    for stub in stubs.values():
        stub.start()
    for name, key in (("oa_key", "sk-oa"), ("an_key", "sk-an"), ("or_key", "sk-or")):
        secrets.save_secret(name, key)
    # Config only: nothing here goes through code.
    doc = {
        "connections": [
            {"name": "openai", "driver": "openai_responses", "base_url": stubs["openai"].base_url,
             "trust": "standard", "secret_ref": "oa_key"},
            {"name": "anthropic", "driver": "anthropic_messages", "base_url": stubs["anthropic"].base_url,
             "trust": "standard", "secret_ref": "an_key"},
            {"name": "openrouter", "driver": "openai_chat", "base_url": stubs["openrouter"].base_url,
             "trust": "standard", "secret_ref": "or_key", "quirks": "openrouter",
             "default_params": {"provider": {"allow_fallbacks": False}}},
            {"name": "ollama-desk", "driver": "openai_chat", "base_url": stubs["ollama-desk"].base_url,
             "trust": "local", "quirks": "ollama"},
            {"name": "ollama-laptop", "driver": "openai_chat", "base_url": stubs["ollama-laptop"].base_url,
             "trust": "local", "quirks": "ollama"},
        ],
        "aliases": {"cheap-json": {"endpoint": "openrouter/vendor/json-only"}, "claude": {"endpoint": "anthropic/claude-x"},
                    "gpt": {"endpoint": "openai/gpt-x"}},
        "policies": {"data_classes": {"sensitive": ["local", "zero_retention"]}},
        "settings": {"retries": 0},
    }
    (layer.data_dir / "models.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    config.forget()
    assert all(r["ok"] for r in models.refresh_catalog().values())
    yield stubs
    for stub in stubs.values():
        stub.stop()


def test_everything_is_connected_at_once_from_config_alone(world):
    ids = {e.id for e in models.list_endpoints()}
    assert {"openai/gpt-x", "anthropic/claude-x", "openrouter/vendor/json-only",
            "ollama-desk/llama3.1:8b", "ollama-laptop/llama3.1:8b"} <= ids
    world["openrouter"].queue(Turn(text="hi"))
    models.generate(ask(requirements=Requirements(pin="cheap-json")))
    assert world["openrouter"].last_body()["provider"] == {"allow_fallbacks": False}


def test_tools_sensitive_cost_goes_local_explains_why_falls_back_and_fails_when_nothing_is_left(world):
    request = ask("my private notes", data_class="sensitive", optimize="cost", tools=TOOLS)
    explanation = models.explain_route(request)
    assert set(explanation.ranked) == {"ollama-desk/llama3.1:8b", "ollama-laptop/llama3.1:8b"}
    rejected = {r.endpoint_id: r.reason for r in explanation.rejected}
    assert rejected == {"openai/gpt-x": "trust_not_allowed", "anthropic/claude-x": "trust_not_allowed",
                        "openrouter/vendor/json-only": "trust_not_allowed"}

    first = explanation.ranked[0]
    first_conn = first.split("/")[0]
    other_conn = "ollama-laptop" if first_conn == "ollama-desk" else "ollama-desk"
    world[first_conn].queue(Turn(text="local answer"))
    assert models.generate(request).provenance.endpoint_id == first

    world[first_conn].stop()  # taken offline
    world[other_conn].queue(Turn(text="the other machine"))
    moved = models.generate(request)
    assert moved.text == "the other machine" and moved.provenance.endpoint_id.startswith(other_conn)
    assert [f.from_endpoint for f in moved.provenance.fallbacks] == [first]
    for name in ("openai", "anthropic", "openrouter"):
        assert "my private notes" not in json.dumps(world[name].requests)  # never, not even on fallback

    world[other_conn].stop()  # nothing allowed is left
    with pytest.raises(errors.Unavailable):
        models.generate(request)
    assert not [r for s in ("openai", "anthropic", "openrouter") for r in world[s].generations()]


def test_strict_output_on_a_json_mode_only_model_fails_with_required_and_is_emulated_with_best_effort(world):
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
    pinned = Requirements(pin="cheap-json")
    with pytest.raises(errors.NoEligibleEndpoint, match="enforce"):
        models.generate(ask(output=OutputSpec("json", schema, "required"), requirements=pinned))
    world["openrouter"].queue(Turn(text='{"n": 3}'))
    response = models.generate(ask(output=OutputSpec("json", schema, "best_effort"), requirements=pinned))
    assert response.data == {"n": 3} and response.report.features["structured_output"] == "emulated"
    assert world["openrouter"].last_body()["response_format"] == {"type": "json_object"}


def test_switching_endpoints_mid_conversation_keeps_ids_and_drops_foreign_reasoning(world):
    world["anthropic"].queue(Turn(tools=[("lookup", {"q": "x"})]))
    base = ask("find x", tools=TOOLS)
    first = models.generate(type(base)(**{**base.__dict__, "requirements": Requirements(pin="claude")}))
    call = first.tool_calls[0]
    assert any(isinstance(i, Sealed) for i in first.items)
    history = (*base.items, *first.items, ToolResult(call.id, call.name, {"found": True}))
    world["openai"].queue(Turn(text="Found it.", reasoning=False))
    second = models.generate(type(base)(**{**base.__dict__, "items": history,
                                           "requirements": Requirements(pin="gpt")}))
    assert second.text == "Found it."
    assert second.report.features["foreign_provider_state"] == "dropped"
    sent = world["openai"].last_body()["input"]
    assert [i["call_id"] for i in sent if i.get("type") in ("function_call", "function_call_output")] == [call.id] * 2
    assert "SIG-abc123" not in json.dumps(sent)
