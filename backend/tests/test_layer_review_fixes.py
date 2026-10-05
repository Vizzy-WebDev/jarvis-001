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


@pytest.fixture
def client_for_models(layer):
    from fastapi.testclient import TestClient

    from jarvis import assembly
    from jarvis.main import create_app

    assembly.reset_for_tests()
    yield TestClient(create_app())
    assembly.reset_for_tests()


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


# --- D3a: a connection that refused the key or the account rests, as a whole --------------------

def test_an_auth_refusal_rests_the_whole_connection_so_the_next_call_skips_it(layer):
    from jarvis.models import state

    configure(layer, [fake_conn("dead", models={"m1": {"capabilities": CHAT}, "m2": {"capabilities": CHAT}}),
                      fake_conn("ok")], settings={"refused_rest_s": 600})
    fake.queue("dead", errors.Auth("dead.test didn't accept the key."))
    fake.queue("ok", fake.reply("one"), fake.reply("two"))
    models.generate(ask())
    assert state.connection_refused("dead")
    ranked = models.explain_route(ask())
    assert ranked.ranked == ("ok/m",)
    assert {r.reason for r in ranked.rejected if r.endpoint_id.startswith("dead/")} == {"connection_refused"}
    assert "dead.test" not in "".join(r.detail for r in ranked.rejected)  # no raw text, plain words
    models.generate(ask())
    assert [c.connection for c in fake.calls()] == ["dead", "ok", "ok"]  # one refusal, not one per model


def test_the_rest_survives_a_restart_and_ends_after_its_cooldown(layer, monkeypatch):
    from jarvis.models import state

    configure(layer, [fake_conn("dead"), fake_conn("ok")], settings={"refused_rest_s": 600})
    fake.queue("dead", errors.Auth("billing"))
    fake.queue("ok", fake.reply("one"))
    models.generate(ask())
    state.flush()
    state.reset()  # what a restart does: read back from the file
    assert state.connection_refused("dead")
    later = state.now() + 601
    monkeypatch.setattr(state, "now", lambda: later)
    assert not state.connection_refused("dead")


@pytest.mark.parametrize("how", ["edit_key", "test", "discover"])
def test_editing_the_key_or_reconnecting_clears_the_rest(layer, client_for_models, how):
    from jarvis.models import state

    configure(layer, [fake_conn("dead", secret_ref="dead_key"), fake_conn("ok")])
    state.refuse_connection("dead", "dead.test didn't accept the key.", 600)
    if how == "edit_key":
        client_for_models.patch("/api/models/dead", json={"apiKey": "a-new-key"})
    else:
        client_for_models.post(f"/api/models/dead/{how}")
    assert not state.connection_refused("dead")


# --- D3b: under Auto, a connection-level refusal falls back; a pin still ends the call ----------

def test_under_auto_an_auth_refusal_falls_back_to_another_connection_and_says_why(layer):
    configure(layer, [fake_conn("dead", models={"m1": {"capabilities": CHAT}, "m2": {"capabilities": CHAT}}),
                      fake_conn("ok")])
    fake.queue("dead", errors.Auth("dead.test refused this for billing or quota reasons."))
    fake.queue("ok", fake.reply("answered"))
    response = models.generate(ask())
    assert response.text == "answered" and response.provenance.endpoint_id == "ok/m"
    assert [c.connection for c in fake.calls()] == ["dead", "ok"]  # the dead connection's other model is skipped
    moved = response.provenance.fallbacks[0]
    assert (moved.from_endpoint, moved.to_endpoint) == ("dead/m1", "ok/m")
    assert "billing or quota" in moved.reason


def test_a_pinned_selection_still_ends_the_call_on_an_auth_refusal(layer):
    configure(layer, [fake_conn("dead"), fake_conn("ok")], aliases={"selected": {"endpoint": "dead/m"}})
    fake.queue("dead", errors.Auth("dead.test didn't accept the key."))
    with pytest.raises(errors.Auth):
        models.generate(ask(requirements=Requirements(pin="selected")))
    assert [c.connection for c in fake.calls()] == ["dead"]


def test_no_auth_fallback_after_the_first_streamed_event(layer):
    from jarvis.models.types import ErrorEvent, TextDelta

    configure(layer, [fake_conn("a"), fake_conn("b")])
    fake.queue("a", fake.broken_after("partial", errors.Auth("a.test didn't accept the key.")))
    fake.queue("b", fake.reply("never"))
    events = list(models.stream(ask()))
    assert isinstance(events[0], TextDelta) and isinstance(events[-1], ErrorEvent)
    assert [c.connection for c in fake.calls()] == ["a"]


def test_other_non_retryable_errors_still_end_the_call_under_auto(layer):
    configure(layer, [fake_conn("a"), fake_conn("b")])
    fake.queue("a", errors.InvalidRequest("a.test couldn't use that request."))
    with pytest.raises(errors.InvalidRequest):
        models.generate(ask())
    assert [c.connection for c in fake.calls()] == ["a"]


# --- D2: only a picture in the current turn needs a model that can see (A1-A3) ------------------

SEES = {**CHAT, "image_in": True}
PICTURE = ImagePart("image/png", "iVBORw0KGgo=")


def _after_a_picture(text: str = "and now just text") -> tuple:
    """A conversation where a picture was shared earlier and the latest message is text."""
    from jarvis.models.types import TextPart as T

    return (Message("user", (T("what is this?"), PICTURE)), Message("assistant", (T("A cat."),)),
            Message("user", (T(text),)))


def test_a_picture_from_earlier_does_not_require_a_model_that_can_see(layer):
    """A1/A2: pinned to a model that can't see, every turn after a picture used to fail."""
    configure(layer, [fake_conn("blind", models={"m": {"capabilities": CHAT}})],
              aliases={"selected": {"endpoint": "blind/m"}})
    fake.queue("blind", fake.reply("fine"))
    response = models.generate(Request(task_class="chat", data_class="personal", items=_after_a_picture(),
                                       requirements=Requirements(pin="selected")))
    assert response.text == "fine"
    sent = fake.calls()[-1].prepared.items
    parts = [p for i in sent if isinstance(i, Message) for p in i.parts]
    assert not any(isinstance(p, ImagePart) for p in parts)
    assert any(isinstance(p, TextPart) and "picture" in p.text for p in parts)  # a short placeholder
    assert response.report.features["image_input"] == "dropped"
    assert any("picture" in w for w in response.report.warnings)


def test_a_picture_in_the_current_turn_still_needs_a_model_that_can_see(layer):
    configure(layer, [fake_conn("blind", models={"m": {"capabilities": CHAT}})],
              aliases={"selected": {"endpoint": "blind/m"}})
    items = (Message("user", (TextPart("what is this?"), PICTURE)),)
    with pytest.raises(errors.NoEligibleEndpoint):
        models.generate(Request(task_class="chat", data_class="personal", items=items,
                                requirements=Requirements(pin="selected")))


def test_a_model_that_can_see_gets_earlier_pictures_unchanged(layer):
    configure(layer, [fake_conn("eyes", models={"m": {"capabilities": SEES}})])
    fake.queue("eyes", fake.reply("fine"))
    response = models.generate(Request(task_class="chat", data_class="personal", items=_after_a_picture()))
    parts = [p for i in fake.calls()[-1].prepared.items if isinstance(i, Message) for p in i.parts]
    assert PICTURE in parts and response.report.features["image_input"] == "native"


def test_under_auto_the_turn_after_a_picture_is_routed_as_a_text_turn(layer):
    """A3's routing half: an earlier picture no longer narrows the next turn to models that see."""
    configure(layer, [fake_conn("blind", models={"m": {"capabilities": CHAT}}),
                      fake_conn("eyes", models={"m": {"capabilities": SEES}})])
    with_picture = models.explain_route(Request(task_class="chat", data_class="personal",
                                                items=(Message("user", (TextPart("look"), PICTURE)),)))
    assert with_picture.ranked == ("eyes/m",)
    after = models.explain_route(Request(task_class="chat", data_class="personal", items=_after_a_picture()))
    assert after.ranked == ("blind/m", "eyes/m")


def test_a_failed_request_does_not_change_how_the_next_message_is_routed(layer):
    configure(layer, [fake_conn("blind", models={"m": {"capabilities": CHAT}}),
                      fake_conn("eyes", models={"m": {"capabilities": SEES}})])
    next_message = Request(task_class="chat", data_class="personal", items=_after_a_picture(),
                           affinity_key="chat-1")
    before = models.explain_route(next_message)
    fake.queue("eyes", errors.InvalidRequest("eyes.test couldn't use that request."))
    with pytest.raises(errors.InvalidRequest):
        models.generate(Request(task_class="chat", data_class="personal", affinity_key="chat-1",
                                items=(Message("user", (TextPart("look"), PICTURE)),)))
    after = models.explain_route(next_message)
    assert after.ranked == before.ranked and after.rejected == before.rejected
    fake.queue("blind", fake.reply("text answer"))
    assert models.generate(next_message).provenance.endpoint_id == "blind/m"


def test_the_boundary_resends_a_stored_picture_and_the_layer_makes_it_a_placeholder(layer):
    """The real path: pictures come back from the stored conversation (`media`)."""
    configure(layer, [fake_conn("blind", models={"m": {"capabilities": CHAT}})])
    stored = [{"role": "user", "text": "what is this?",
               "media": [{"kind": "image", "mimeType": "image/png", "dataBase64": "iVBORw0KGgo="}]},
              {"role": "assistant", "text": "A cat."},
              {"role": "user", "text": "thanks"}]
    fake.queue("blind", fake.reply("you're welcome"))
    response = models.generate(Request(task_class="chat", data_class="personal",
                                       items=boundary.items_from_conversation(stored)))
    assert response.text == "you're welcome"
