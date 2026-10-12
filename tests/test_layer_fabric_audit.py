"""Model Fabric audit fixes: a forbidden model is not a dead key, honest kinds for id-only listings,
a gateway's own reasoning levels, and a stale effort is never sent."""

from __future__ import annotations

import pytest

from jarvis import models, prefs
from jarvis.models import errors, execute, settings, state
from jarvis.models.drivers import _wire, fake, openai_chat
from jarvis.models.prepared import Discovered
from jarvis.models.types import Requirements

from layer_helpers import CHAT, ask, configure, fake_conn, layer  # noqa: F401


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(execute, "sleep", lambda s: None)


def two_models():
    return fake_conn("c", models={"m1": {"capabilities": CHAT}, "m2": {"capabilities": CHAT}})


def openai_conn():
    return {"name": "oa", "driver": "openai_responses", "base_url": "https://oa.test/v1", "trust": "standard"}


# --- a model the account may not use is the model's problem, not the key's ----------------------

@pytest.mark.parametrize("status,words", [
    (403, "Project does not have access to model `gpt-x`"),
    (404, "The model gpt-x is not available for your account; you do not have access to it."),
    (400, "This model is only supported in v1/responses and not in v1/chat/completions."),
])
def test_a_model_the_account_cannot_use_is_endpoint_scoped(status, words):
    err = _wire.error_for(status, words, "https://api.example.test/v1/chat/completions")
    assert isinstance(err, errors.ModelNotAllowed)
    assert err.scope == "endpoint" and err.recovery == "user" and not err.retryable
    assert words not in str(err)


def test_wording_that_may_mean_the_model_is_missing_stays_a_missing_model():
    words = "The model `gpt-x` does not exist or you do not have access to it."
    assert isinstance(_wire.error_for(404, words, "https://api.example.test/v1"), errors.ModelNotFound)


def test_a_real_bad_key_is_still_a_connection_refusal():
    assert isinstance(_wire.error_for(401, "Incorrect API key provided", "https://api.example.test/v1"), errors.Auth)
    assert isinstance(_wire.error_for(403, "Forbidden", "https://api.example.test/v1"), errors.Auth)


def test_a_forbidden_model_is_set_aside_and_the_connection_keeps_working(layer):
    configure(layer, [two_models()])
    fake.queue("c", errors.ModelNotAllowed("will not serve that model"))
    fake.queue("c", fake.reply("the sibling answers"))
    response = models.generate(ask())
    assert response.provenance.endpoint_id == "c/m2"
    assert not state.connection_refused("c")
    assert state.endpoint_rest("c/m1")["kind"] == "unusable"
    ranked = models.explain_route(ask())
    assert ranked.ranked == ("c/m2",)
    assert [r.reason for r in ranked.rejected] == ["model_not_allowed"]
    assert settings.availability().state == "ok"


def test_a_forbidden_model_that_was_pinned_ends_the_call_without_resting_the_key(layer):
    configure(layer, [two_models()], aliases={"selected": {"endpoint": "c/m1"}})
    fake.queue("c", errors.ModelNotAllowed("will not serve that model"))
    with pytest.raises(errors.ModelNotAllowed):
        models.generate(ask(requirements=Requirements(pin="selected")))
    assert not state.connection_refused("c")


def test_reconnecting_clears_the_set_aside(layer):
    configure(layer, [two_models()])
    state.rest_endpoint("c/m1", 3600, reason="no access", kind="unusable")
    state.clear_rests("c")
    assert not state.endpoint_rest("c/m1")


# --- an id-only listing no longer makes every model a chat model --------------------------------

@pytest.mark.parametrize("model_id,kind", [
    ("text-embedding-3-small", "embedding"), ("whisper-1", "transcription"), ("tts-1", "speech"),
    ("dall-e-3", "image"), ("omni-moderation-latest", "moderation"), ("gpt-4o-realtime-preview", "realtime"),
    ("gpt-4.1", "chat"),
])
def test_an_id_only_openai_listing_gets_honest_kinds(layer, model_id, kind):
    configure(layer, [openai_conn()])
    state.record_discovery("oa", [Discovered(model_id)])
    found = {e.model_id: e for e in models.list_endpoints()}[model_id]
    assert found.kind == kind


def test_a_non_chat_model_cannot_be_chosen_to_talk_with(layer):
    configure(layer, [openai_conn()])
    state.record_discovery("oa", [Discovered("text-embedding-3-small"), Discovered("gpt-4.1")])
    with pytest.raises(ValueError, match="embedding"):
        settings.select("oa", "text-embedding-3-small", None)
    assert settings.select("oa", "gpt-4.1", None)["modelId"] == "gpt-4.1"


# --- a gateway's own reasoning description ------------------------------------------------------

REASONING_ROW = {
    "id": "anthropic/claude-x", "context_length": 200000,
    "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"], "tokenizer": "Claude"},
    "pricing": {"prompt": "0.000003", "completion": "0.000015"},
    "supported_parameters": ["tools", "reasoning", "include_reasoning"],
    "reasoning": {"supported_efforts": ["xhigh", "high", "medium", "low", "none"], "default_effort": "medium",
                  "mandatory": False},
}


def test_openrouters_reasoning_object_becomes_levels_weakest_first():
    found = openai_chat._listed(REASONING_ROW, {"upstream_from_id": True})
    assert found.capabilities["reasoning_levels"] == ["low", "medium", "high", "xhigh"]
    assert found.capabilities["reasoning_default"] == "medium"
    assert found.capabilities["reasoning_can_disable"] is True
    assert found.upstream == "anthropic"


def test_a_model_whose_reasoning_is_mandatory_cannot_be_switched_off():
    row = {**REASONING_ROW, "reasoning": {"supported_efforts": ["high", "low"], "default_effort": "high",
                                          "mandatory": True}}
    caps = openai_chat._listed(row, {}).capabilities
    assert caps["reasoning_levels"] == ["low", "high"] and caps["reasoning_can_disable"] is False


def test_a_default_the_model_does_not_list_is_not_believed():
    row = {**REASONING_ROW, "reasoning": {"supported_efforts": ["low", "high"], "default_effort": "ultra"}}
    assert "reasoning_default" not in openai_chat._listed(row, {}).capabilities


def test_the_upstream_comes_from_the_id_only_where_the_quirk_says_so():
    plain = {"id": "vendor/model"}
    assert openai_chat._listed(plain, {"upstream_from_id": True}).upstream == "vendor"
    assert openai_chat._listed(plain, {}).upstream is None
    assert openai_chat._listed({"id": "model"}, {"upstream_from_id": True}).upstream is None


def test_a_router_row_priced_minus_one_has_no_price():
    row = {"id": "openrouter/auto", "pricing": {"prompt": "-1", "completion": "-1"}}
    assert openai_chat._listed(row, {"upstream_from_id": True}).pricing is None


# --- effort: only a level the chosen model lists is ever kept or sent ---------------------------

def test_an_invented_effort_level_is_chosen_and_kept(layer):
    configure(layer, [fake_conn("c")])
    state.record_discovery("c", [Discovered("m", capabilities={**CHAT, "reasoning_levels": ["low", "xhigh"]})])
    assert settings.select("c", "m", "xhigh")["effort"] == "xhigh"
    assert settings.effort() == "xhigh"
    assert settings.select("c", "m", "medium")["effort"] is None


def test_a_saved_effort_the_model_no_longer_lists_is_not_sent(layer):
    configure(layer, [fake_conn("c")])
    state.record_discovery("c", [Discovered("m", capabilities={**CHAT, "reasoning_levels": ["low", "high"]})])
    settings.select("c", "m", "high")
    assert settings.effort() == "high"
    state.record_discovery("c", [Discovered("m", capabilities={**CHAT, "reasoning_levels": ["low"]})])
    assert prefs.get_prefs()["selectedEffort"] == "high"
    assert settings.effort() is None


# --- a model that says it cannot use tools is not offered for chatting --------------------------

def test_a_model_listed_as_unable_to_use_tools_is_refused_but_unknown_stays_selectable(layer):
    configure(layer, [fake_conn("c")])
    state.record_discovery("c", [
        Discovered("no-tools", capabilities={**CHAT, "tools": False}),
        Discovered("unknown", capabilities={}),
    ])
    with pytest.raises(ValueError, match="tool"):
        settings.select("c", "no-tools", None)
    assert settings.select("c", "unknown", None)["modelId"] == "unknown"
    view = {m["id"]: m for c in settings.connections_view() for m in c["models"]} if hasattr(settings, "connections_view") else {}
    if view:
        assert view["no-tools"]["chatProblem"] and view["unknown"]["chatProblem"] is None


# --- Gemini: methods a model lacks never leak out as its "kind" ---------------------------------

def test_a_gemini_model_without_generate_content_has_a_plain_kind(monkeypatch):
    from jarvis.models.drivers import gemini_generate
    from jarvis.models.prepared import ConnInfo

    listing = {"models": [
        {"name": "models/m-live", "supportedGenerationMethods": ["bidiGenerateContent"]},
        {"name": "models/m-embed", "supportedGenerationMethods": ["embedContent"]},
        {"name": "models/m-chat", "supportedGenerationMethods": ["generateContent", "countTokens"]},
    ]}
    monkeypatch.setattr(gemini_generate._wire, "get_json", lambda *a, **kw: listing)
    found = {d.model_id: d.capabilities.get("kind") for d in gemini_generate.discover(ConnInfo("g", "http://g.test", "k", {}))}
    assert found == {"m-live": "other", "m-embed": "embedding", "m-chat": None}
