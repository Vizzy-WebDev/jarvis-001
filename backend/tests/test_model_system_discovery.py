import pytest

from jarvis.model_system.discovery import fetch, for_picker, normalise, reconcile, refresh
from jarvis.model_system.providers import AuthMethod, ProviderKind, add_provider
from jarvis.model_system.registry import add_model, get_model

from stub_openai_server import StubModelServer


@pytest.fixture
def stub():
    server = StubModelServer()
    server.base_url = server.start()
    yield server
    server.stop()


@pytest.fixture
def provider(scratch, stub):
    return add_provider(label="Stub", kind=ProviderKind.OPENAI_COMPATIBLE,
                        adapter="openai_compatible", base_url=stub.base_url,
                        auth_method=AuthMethod.NONE, key_required=False)


def test_normalise_reads_reasoning_from_supported_parameters():
    listed = normalise([{"model": "m1", "supported_parameters": ["reasoning_effort"]}])
    assert listed[0].discovered["reasoning"]["kind"] == "tiers"


def test_normalise_reasoning_none_when_parameters_listed_without_it():
    listed = normalise([{"model": "m1", "supported_parameters": ["temperature"]}])
    assert listed[0].discovered["reasoning"]["kind"] == "none"


def test_normalise_no_parameters_reported_leaves_reasoning_unset():
    listed = normalise([{"model": "m1"}])
    assert "reasoning" not in listed[0].discovered


def test_normalise_maps_capability_aliases():
    listed = normalise([{"model": "m1", "capabilities": ["tool_use", "vision"]}])
    assert listed[0].discovered["capabilities"] == {"tool_calling": True, "vision": True}


def test_fetch_unknown_provider_is_a_clean_error(scratch):
    listed, error = fetch("does-not-exist")
    assert listed == []
    assert error is not None


def test_fetch_reads_the_stub_listing(scratch, provider, stub):
    stub.models = [{"id": "stub-model", "context_length": 32000}]
    listed, error = fetch(provider.id)
    assert error is None
    assert listed[0].native_model_id == "stub-model"
    assert listed[0].discovered["contextWindow"] == 32000


def test_reconcile_finds_added_present_and_retired(scratch, provider, stub):
    configured = add_model(provider_id=provider.id, native_model_id="gone")
    still_here = add_model(provider_id=provider.id, native_model_id="stub-model")
    stub.models = [{"id": "stub-model"}, {"id": "brand-new"}]
    listed, _ = fetch(provider.id)
    result = reconcile(provider.id, listed)
    assert {row.native_model_id for row in result.added} == {"brand-new"}
    assert [p.model_id for p in result.present] == [still_here.id]
    assert result.retired == (configured.id,)


def test_refresh_marks_a_missing_model_retired_and_restores_it(scratch, provider, stub):
    model = add_model(provider_id=provider.id, native_model_id="stub-model")
    stub.models = []
    refresh(provider.id)
    assert get_model(model.id).status == "retired"

    stub.models = [{"id": "stub-model"}]
    refresh(provider.id)
    assert get_model(model.id).status == "current"


def test_refresh_refreshes_context_window_for_a_configured_model(scratch, provider, stub):
    model = add_model(provider_id=provider.id, native_model_id="stub-model")
    stub.models = [{"id": "stub-model", "context_length": 200000}]
    refresh(provider.id)
    assert get_model(model.id).context_window == 200000


def test_refresh_never_deletes_a_model_only_marks_it(scratch, provider, stub):
    model = add_model(provider_id=provider.id, native_model_id="stub-model")
    stub.models = []
    refresh(provider.id)
    assert get_model(model.id) is not None


def test_refresh_does_not_configure_added_models_automatically(scratch, provider, stub):
    from jarvis.model_system.registry import list_models

    stub.models = [{"id": "never-added"}]
    refresh(provider.id)
    assert list_models(provider.id) == []


def test_refresh_unreachable_provider_reports_error_and_changes_nothing(scratch, provider, stub):
    model = add_model(provider_id=provider.id, native_model_id="stub-model")
    stub.stop()  # now unreachable
    result = refresh(provider.id)
    assert result.error is not None
    assert get_model(model.id).status != "retired"


def test_for_picker_attaches_family_from_the_seed():
    picked = for_picker([{"model": "claude-opus-4-20260101"}, {"model": "totally-unknown"}])
    assert picked[0]["family"] == "claude-opus"
    assert picked[1]["family"] is None
