import pytest

from jarvis.model_system.capabilities import Support
from jarvis.model_system.parameters import Param
from jarvis.model_system.providers import ProviderKind, add_provider
from jarvis.model_system.reasoning import Effort, ReasoningKind
from jarvis.model_system.registry import (
    add_model, delete_model, get_model, list_models, match_seed,
    record_discovery, record_parameter_refusal, update_model,
)


@pytest.fixture
def provider(scratch):
    return add_provider(label="Test", kind=ProviderKind.OPENAI_COMPATIBLE,
                        adapter="openai_compatible")


def test_empty_seed_still_resolves_a_usable_model(scratch, provider):
    """The real specification of the seed table: a model matching nothing must
    still come back fully resolved, everything unknown, nothing invented."""
    model = add_model(provider_id=provider.id, native_model_id="totally-unknown-model-xyz")
    assert model.capabilities.tool_calling is Support.UNKNOWN
    assert model.reasoning.kind is ReasoningKind.UNKNOWN
    assert model.quality is None
    assert model.family is None
    # An unrecognised model's maker falls back to the provider it was
    # configured under — the same fallback the old catalog used.
    assert model.maker == provider.id


def test_maker_is_the_lineages_owner_not_the_configured_provider(scratch):
    """A model added under a reseller/aggregator provider is still serving
    its real maker's model, with that maker's real cost — the key spend and
    pricing are filed under must name the maker, not the connection."""
    reseller = add_provider(label="Some Reseller", kind=ProviderKind.AGGREGATOR,
                            adapter="openai_compatible")
    model = add_model(provider_id=reseller.id, native_model_id="claude-sonnet-5-20260201")
    assert model.maker == "anthropic"
    assert model.provider.id == reseller.id


def test_seed_matches_a_known_family(scratch, provider):
    model = add_model(provider_id=provider.id, native_model_id="claude-opus-4-20260101")
    assert model.family == "claude-opus"
    assert model.capabilities.tool_calling is Support.YES
    assert model.reasoning.kind is ReasoningKind.BUDGET
    assert model.quality == 5


def test_match_seed_is_pattern_based_not_exact(scratch):
    # A dated snapshot id must still match — this is the whole reason the seed
    # is keyed by pattern rather than exact id.
    assert match_seed("claude-sonnet-5-20260315") is not None
    assert match_seed("something-nobody-has-heard-of") is None


def test_duplicate_model_under_same_provider_is_rejected(scratch, provider):
    add_model(provider_id=provider.id, native_model_id="gpt-5.6-luna")
    with pytest.raises(ValueError):
        add_model(provider_id=provider.id, native_model_id="gpt-5.6-luna")


def test_same_model_under_two_providers_is_two_independent_rows(scratch):
    p1 = add_provider(label="Route A", kind=ProviderKind.OPENAI_COMPATIBLE,
                      adapter="openai_compatible")
    p2 = add_provider(label="Route B", kind=ProviderKind.AGGREGATOR,
                      adapter="openai_compatible")
    m1 = add_model(provider_id=p1.id, native_model_id="gpt-5.6-luna")
    m2 = add_model(provider_id=p2.id, native_model_id="gpt-5.6-luna")
    assert m1.id != m2.id


def test_add_model_unknown_provider_raises(scratch):
    with pytest.raises(KeyError):
        add_model(provider_id="no-such-provider", native_model_id="x")


def test_discovered_facts_are_read_back(scratch, provider):
    model = add_model(provider_id=provider.id, native_model_id="local-llama")
    updated = record_discovery(model.id, {
        "contextWindow": 128000, "capabilities": {"vision": True},
        "supportedParams": {"seed": "yes"},
    })
    assert updated.context_window == 128000
    assert updated.capabilities.vision is Support.YES
    assert updated.parameters[Param.SEED] is Support.YES


def test_user_override_wins_over_discovered(scratch, provider):
    model = add_model(provider_id=provider.id, native_model_id="local-llama")
    record_discovery(model.id, {"capabilities": {"vision": True}})
    overridden = update_model(model.id, {"capability_overrides": {"vision": "no"}})
    assert overridden.capabilities.vision is Support.NO


def test_record_parameter_refusal_is_remembered(scratch, provider):
    model = add_model(provider_id=provider.id, native_model_id="gpt-5.6-luna")
    record_parameter_refusal(model.id, Param.SEED)
    refreshed = get_model(model.id)
    assert refreshed.parameters[Param.SEED] is Support.NO


def test_delete_model_removes_it(scratch, provider):
    model = add_model(provider_id=provider.id, native_model_id="gone-tomorrow")
    delete_model(model.id)
    assert get_model(model.id) is None


def test_list_models_filters_by_provider(scratch):
    p1 = add_provider(label="A", kind=ProviderKind.OPENAI_COMPATIBLE, adapter="openai_compatible")
    p2 = add_provider(label="B", kind=ProviderKind.OPENAI_COMPATIBLE, adapter="openai_compatible")
    add_model(provider_id=p1.id, native_model_id="m1")
    add_model(provider_id=p2.id, native_model_id="m2")
    assert [m.native_model_id for m in list_models(p1.id)] == ["m1"]
    assert len(list_models()) == 2


def test_as_dict_never_contains_a_secret(scratch, provider):
    model = add_model(provider_id=provider.id, native_model_id="claude-haiku-4-5")
    payload = model.as_dict()
    assert "credential_ref" not in str(payload)


def test_pinned_reads_the_shape_of_a_dated_snapshot_id(scratch, provider):
    floating = add_model(provider_id=provider.id, native_model_id="claude-opus-4")
    pinned = add_model(provider_id=provider.id, native_model_id="claude-opus-4-20260101")
    assert floating.pinned is Support.NO
    assert pinned.pinned is Support.YES
    assert floating.as_dict()["pinned"] == "no"


def test_capability_provenance_follows_user_over_discovered_over_catalog(scratch, provider):
    model = add_model(provider_id=provider.id, native_model_id="claude-opus-4-20260101")
    # Catalog seed alone: tool_calling is known from the family match.
    assert model.provenance["capabilities.tool_calling"] == "catalog"
    assert model.provenance["capabilities.vision"] == "default"

    record_discovery(model.id, {"capabilities": {"vision": True}})
    discovered = get_model(model.id)
    assert discovered.provenance["capabilities.vision"] == "discovered"

    overridden = update_model(model.id, {"capability_overrides": {"vision": "no"}})
    assert overridden.provenance["capabilities.vision"] == "user"
