"""Fixes from the read-only review of the rebuilt model layer (the A/B/C/D findings).

Each test names the finding it pins. The A/B/C scenarios are the ones the review
reproduced against stubs: a chat stuck after a picture, a dead connection costing a
failed message per model, an error with no separator, a selected model that acts as
a hard pin for background work, and gateway models with no family or upstream.
"""

from __future__ import annotations

import httpx
import pytest

from jarvis import models
from jarvis.models import boundary, errors, execute
from jarvis.models.drivers import _wire
from jarvis.models.drivers import fake
from jarvis.models.types import ImagePart, Message, Request, Requirements, Section, TextPart

from layer_helpers import CHAT, ask, configure, fake_conn, layer  # noqa: F401


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(execute, "sleep", lambda s: None)


# --- plain-language errors (A5) -----------------------------------------------------------------

RAW = "Incorrect API key provided: sk-abc. You can find your API key at https://example.test/keys"


@pytest.mark.parametrize("status,words,kind", [
    (401, RAW, errors.Auth),
    (403, "You exceeded your current quota, please check your plan and billing details.", errors.Auth),
    (429, "Rate limit reached for requests", errors.RateLimited),
    (400, "The requested combination of response modalities (TEXT) is not supported by the model.",
     errors.InvalidRequest),
    (503, "This model is currently experiencing high demand.", errors.Unavailable),
    (404, "The model `stub-model-a` does not exist", errors.InvalidRequest),
])
def test_an_http_failure_is_said_plainly_and_the_providers_words_are_kept_aside(status, words, kind):
    err = _wire.error_for(status, words, "https://api.example.test/v1/chat/completions")
    assert isinstance(err, kind)
    assert words not in str(err) and "(" not in str(err)  # no raw provider text, no status code
    assert str(err).endswith(".")
    assert err.detail["provider_words"] == words and err.detail["status"] == status


def test_a_quota_refusal_on_403_reads_as_billing_not_as_a_bad_key():
    err = _wire.error_for(403, "You exceeded your current quota", "https://api.example.test/v1")
    assert isinstance(err, errors.Auth) and "billing or quota" in str(err) and "key" not in str(err)


def test_a_broken_connection_does_not_show_the_raw_exception():
    err = _wire.network_error(httpx.ReadError("[WinError 10054] An existing connection was forcibly closed"),
                              "https://api.example.test/v1")
    assert "WinError" not in str(err) and "api.example.test" in str(err)
    assert "WinError" in err.detail["provider_words"]


def test_the_stayed_on_your_model_note_is_its_own_sentence_and_names_the_model():
    err = errors.Auth("api.example.test didn't accept the key", endpoint_id="conn/some-model")
    said = str(boundary.failure(err, pinned_selection=True))
    assert "key. Jarvis stays on the model you picked (some-model)." in said


def test_a_missing_capability_is_said_in_plain_words(layer):
    configure(layer, [fake_conn("a", models={"m": {"capabilities": CHAT}})], aliases={"pick": {"endpoint": "a/m"}})
    request = Request(task_class="chat", data_class="personal", requirements=Requirements(pin="pick"),
                      items=(Message("user", (TextPart("what is this?"), ImagePart("image/png", "AAAA"))),))
    with pytest.raises(errors.NoEligibleEndpoint) as caught:
        models.generate(request)
    assert "image_in" not in str(caught.value) and "picture" in str(caught.value)


def test_the_trace_keeps_the_providers_words_for_diagnosis(layer):
    configure(layer, [fake_conn("a")])
    fake.queue("a", errors.Auth("api.example.test didn't accept the key.",
                                detail={"provider_words": "Incorrect API key provided"}))
    seen = []
    execute.observers.append(seen.append)
    try:
        with pytest.raises(errors.Auth) as caught:
            models.generate(ask())
    finally:
        execute.observers.remove(seen.append)
    assert "Incorrect API key" not in str(caught.value)
    assert "Incorrect API key provided" in seen[-1].attempts[-1].message


# --- Gemini discovery: a speech-only model can't answer in text (item 20) -----------------------

def test_gemini_discovery_marks_a_speech_only_model_as_unable_to_answer_in_text(monkeypatch):
    from jarvis.models.drivers import gemini_generate
    from jarvis.models.prepared import ConnInfo

    listing = {"models": [
        {"name": "models/gemini-2.5-flash-preview-tts", "supportedGenerationMethods": ["generateContent"],
         "inputTokenLimit": 8192, "outputTokenLimit": 16384, "displayName": "Gemini 2.5 Flash Preview TTS"},
        {"name": "models/gemini-3.8-flash-lite-tts", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-2.5-flash-image", "supportedGenerationMethods": ["generateContent"]},
    ]}
    monkeypatch.setattr(gemini_generate._wire, "get_json", lambda *a, **kw: listing)
    found = {d.model_id: d.capabilities for d in gemini_generate.discover(ConnInfo("g", "http://g.test", "k", {}))}
    assert found["gemini-2.5-flash-preview-tts"].get("text_in") is False
    assert found["gemini-3.8-flash-lite-tts"].get("text_in") is False
    assert "text_in" not in found["gemini-3.8-flash"] and "text_in" not in found["gemini-2.5-flash-image"]


def test_a_speech_only_gemini_model_is_never_routed_a_chat_turn(layer):
    from jarvis.models import state
    from jarvis.models.prepared import Discovered

    configure(layer, [{"name": "g", "driver": "gemini_generate", "base_url": "http://g.test", "trust": "standard",
                       "models": {}}])
    state.record_discovery("g", [Discovered("gemini-2.5-flash-preview-tts", capabilities={"text_in": False}),
                                 Discovered("gemini-3.8-flash")])
    ranked = models.explain_route(ask()).ranked
    assert ranked == ("g/gemini-3.8-flash",)


# --- gateways: family and upstream from the listing (C8-C9) -------------------------------------

def _gateway_row(model_id: str, tokenizer: str) -> dict:
    return {"id": model_id, "name": model_id, "context_length": 200000,
            "architecture": {"tokenizer": tokenizer, "input_modalities": ["text"], "output_modalities": ["text"]},
            "pricing": {"prompt": "0.000003", "completion": "0.000015"},
            "supported_parameters": ["tools", "reasoning"]}


def test_a_gateway_listing_gives_each_model_its_family_and_upstream():
    from jarvis.models.drivers import openai_chat

    found = openai_chat._listed(_gateway_row("anthropic/claude-sonnet-4.6", "Claude"))
    assert (found.family, found.upstream) == ("claude", "anthropic")
    router = openai_chat._listed(_gateway_row("openrouter/auto", "Router"))
    assert router.family is None  # a router isn't a model family
    plain = openai_chat._listed({"id": "deepseek-ai/DeepSeek-V4-Flash"})  # a plain server: nothing is guessed
    assert (plain.family, plain.upstream) == (None, None)


def _gateway(layer, rows):
    from jarvis.models import state
    from jarvis.models.drivers import openai_chat

    configure(layer, [fake_conn("gw", trust="standard", models={})])
    state.record_discovery("gw", [openai_chat._listed(r) for r in rows])


def test_a_gateway_falls_back_to_another_upstream_first(layer):
    _gateway(layer, [_gateway_row("anthropic/claude-a", "Claude"), _gateway_row("anthropic/claude-b", "Claude"),
                     _gateway_row("google/gemini-c", "Gemini")])
    fake.queue("gw", *[errors.Unavailable("vendor down")] * 3, fake.reply("other vendor"))
    response = models.generate(ask(requirements=Requirements(pin=None)))
    assert response.provenance.endpoint_id == "gw/google/gemini-c"


def test_a_gateway_model_can_fall_back_within_its_family_when_family_change_is_forbidden(layer):
    _gateway(layer, [_gateway_row("anthropic/claude-a", "Claude"), _gateway_row("google/gemini-c", "Gemini"),
                     _gateway_row("anthropic/claude-b", "Claude")])
    fake.queue("gw", *[errors.Unavailable("down")] * 3, fake.reply("same family"))
    response = models.generate(ask(requirements=Requirements(allow_family_change=False)))
    assert response.provenance.endpoint_id == "gw/anthropic/claude-b"


def test_a_family_alias_resolves_to_gateway_models_and_their_prompt_profile_applies(layer):
    _gateway(layer, [_gateway_row("anthropic/claude-a", "Claude"), _gateway_row("google/gemini-c", "Gemini")])
    configure(layer, [fake_conn("gw", trust="standard", models={})], aliases={"any-claude": {"family": "claude"}})
    request = Request(task_class="chat", data_class="personal", requirements=Requirements(pin="any-claude"),
                      items=(Message("user", (TextPart("hi"),)),), instructions=(Section("rules", "be brief"),))
    fake.queue("gw", fake.reply("ok"))
    response = models.generate(request)
    assert response.provenance.endpoint_id == "gw/anthropic/claude-a"
    assert "<rules>" in fake.calls()[-1].prepared.system[0].text  # the claude profile: XML tags
