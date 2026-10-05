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
from jarvis.models.types import ImagePart, Message, Request, Requirements, TextPart

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
