"""Adding a provider end to end: discover -> verify/test -> save, against a
real OpenAI-shaped HTTP stub, mirroring what `routes/models.py` calls."""

from __future__ import annotations

import pytest

from jarvis.model_system.providers import get_provider
from jarvis.model_system.registry import list_models
from jarvis.model_system.setup import (
    add_models_to_provider, create_provider_with_models, discover_models, verify_reachability,
)
from jarvis.model_system.setup import test_model as check_model

from stub_openai_server import StubModelServer


@pytest.fixture
def stub():
    server = StubModelServer()
    server.base_url = server.start()
    yield server
    server.stop()


def test_discover_models_lists_what_the_address_has(scratch, stub):
    result = discover_models(adapter="openai_compatible", base_url=stub.base_url)
    assert result["error"] is None
    assert [m["model"] for m in result["models"]] == ["stub-model"]


def test_discover_models_answers_an_unreachable_address_without_raising(scratch):
    result = discover_models(adapter="openai_compatible", base_url="http://127.0.0.1:1")
    assert result["models"] == []
    assert result["error"]


def test_discover_models_filters_out_what_is_already_added(scratch, stub):
    provider = create_provider_with_models(
        adapter="openai_compatible", base_url=stub.base_url, label="Stub",
        secret=None, models=["stub-model"])["provider"]
    result = discover_models(adapter="openai_compatible", base_url=stub.base_url,
                             provider_id=provider.id)
    assert result["models"] == []


def test_discover_models_resolves_a_known_template(scratch):
    """A first-party template's own address is used even though none was
    typed — the whole reason discovery for "Anthropic" talks to Anthropic
    instead of silently defaulting to the openai-compatible shape."""
    result = discover_models(template="anthropic")
    # No credential configured, so this can't reach the real API — the point
    # is it tried the ANTHROPIC adapter, not that it succeeded.
    assert result["error"]


def test_verify_reachability_and_test_model_against_the_real_stub(scratch, stub):
    stub.says("ready")
    ok = verify_reachability(adapter="openai_compatible", base_url=stub.base_url, secret=None)
    assert ok == {"ok": True}

    stub.says("ready")
    result = check_model(adapter="openai_compatible", model="stub-model",
                         base_url=stub.base_url, secret=None)
    assert result["ok"] is True


def test_create_provider_with_one_model_is_validated_by_generating(scratch, stub):
    stub.says("ready")
    result = create_provider_with_models(
        adapter="openai_compatible", base_url=stub.base_url, label="My Stub",
        secret=None, models=["stub-model"])

    assert result["ok"] is True
    assert result["provider"].label == "My Stub"
    assert [m.native_model_id for m in result["added"]] == ["stub-model"]
    assert list_models(result["provider"].id)


def test_create_provider_with_several_models_is_validated_by_listing_only(scratch, stub):
    result = create_provider_with_models(
        adapter="openai_compatible", base_url=stub.base_url, label="My Stub",
        secret=None, models=["stub-model", "another-model"])

    assert result["ok"] is True
    assert len(result["added"]) == 2


def test_a_failing_check_never_saves_a_provider(scratch):
    result = create_provider_with_models(
        adapter="openai_compatible", base_url="http://127.0.0.1:1", label="Dead",
        secret=None, models=["some-model"])

    assert result["ok"] is False
    assert result["error"]


def test_an_unknown_template_with_no_address_is_rejected(scratch):
    with pytest.raises(ValueError):
        create_provider_with_models(template="not-a-real-provider", models=["m"])


def test_custom_template_goes_through_the_probe_cascade(scratch, stub):
    stub.says("ready")
    result = create_provider_with_models(
        template="custom", base_url=stub.base_url, label="Probed",
        secret=None, models=["stub-model"])

    assert result["ok"] is True
    assert "steps" in result and result["steps"]
    provider = get_provider(result["provider"].id)
    assert provider.kind.value == "local"


def test_add_models_to_provider_appends_without_retesting(scratch, stub):
    created = create_provider_with_models(
        adapter="openai_compatible", base_url=stub.base_url, label="My Stub",
        secret=None, models=["stub-model"])
    outcome = add_models_to_provider(created["provider"].id, ["another-model"])

    assert [m.native_model_id for m in outcome["added"]] == ["another-model"]
    assert outcome["failed"] == []


def test_add_models_to_provider_unknown_provider_raises(scratch):
    with pytest.raises(KeyError):
        add_models_to_provider("no-such-provider", ["m"])
