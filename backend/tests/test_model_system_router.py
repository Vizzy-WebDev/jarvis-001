import pytest

from jarvis.model_system.capabilities import Support
from jarvis.model_system.errors import ErrorKind
from jarvis.model_system.health import record_failure
from jarvis.model_system.providers import AuthMethod, ProviderKind, add_provider
from jarvis.model_system.registry import add_model, update_model
from jarvis.model_system.request import Modality, Preferences, Requirements, Role
from jarvis.model_system.router import explain_exclusions, rank, select


@pytest.fixture
def provider(scratch):
    return add_provider(label="Test", kind=ProviderKind.OPENAI_COMPATIBLE, adapter="openai_compatible",
                        auth_method=AuthMethod.NONE, key_required=False)


def test_a_disabled_model_is_excluded(scratch, provider):
    model = add_model(provider_id=provider.id, native_model_id="m1")
    update_model(model.id, {"enabled": False})
    ranked = rank(Requirements(), Preferences())
    assert ranked == []
    assert explain_exclusions(Requirements())["counts"]["disabled"] == 1


def test_a_model_needing_a_key_it_does_not_have_is_excluded(scratch):
    p = add_provider(label="Needs Key", kind=ProviderKind.NATIVE, adapter="anthropic",
                     auth_method=AuthMethod.API_KEY, key_required=True)
    add_model(provider_id=p.id, native_model_id="claude-opus-4")
    ranked = rank(Requirements(), Preferences())
    assert ranked == []
    assert explain_exclusions(Requirements())["counts"]["needs_key"] == 1


def test_unknown_capability_is_offered_not_excluded(scratch, provider):
    """A model nobody has asked about is offered and allowed to fail
    honestly — only a confirmed NO excludes."""
    add_model(provider_id=provider.id, native_model_id="unknown-model")
    ranked = rank(Requirements(needs_tools=True), Preferences())
    assert len(ranked) == 1


def test_a_confirmed_no_excludes(scratch, provider):
    model = add_model(provider_id=provider.id, native_model_id="text-only")
    update_model(model.id, {"capability_overrides": {"tool_calling": "no"}})
    ranked = rank(Requirements(needs_tools=True), Preferences())
    assert ranked == []


def test_web_search_must_be_certain_regardless_of_spelling(scratch, provider):
    """A camelCase requirement (as arrives over the wire, or from an older
    caller) must be excluded exactly like the canonical spelling — a
    differently-spelled requirement silently bypassing MUST_BE_CERTAIN was a
    real, confirmed bug."""
    add_model(provider_id=provider.id, native_model_id="unknown-model")
    assert rank(Requirements(capabilities={"webSearch": True}), Preferences()) == []
    assert explain_exclusions(Requirements(capabilities={"webSearch": True}))["counts"] == {
        "unproven_webSearch": 1}


def test_web_search_unknown_is_excluded_the_one_exception(scratch, provider):
    """Web search fails SILENTLY when absent — a model that cannot search
    answers from memory, fluently, citing nothing — so UNKNOWN is not good
    enough here, unlike every other capability."""
    add_model(provider_id=provider.id, native_model_id="unknown-model")
    ranked = rank(Requirements(capabilities={"web_search": True}), Preferences())
    assert ranked == []
    assert explain_exclusions(Requirements(capabilities={"web_search": True}))["counts"] == {
        "unproven_web_search": 1}


def test_a_cheap_text_only_model_never_wins_an_image_request(scratch, provider):
    cheap = add_model(provider_id=provider.id, native_model_id="cheap-text-only")
    update_model(cheap.id, {"capability_overrides": {"vision": "no"}, "quality": 5})
    ranked = rank(Requirements(modalities_in=frozenset({Modality.IMAGE})), Preferences())
    assert ranked == []  # the ONLY model configured cannot see, so nothing is eligible


def test_vision_requirement_admits_a_model_that_can_see(scratch, provider):
    blind = add_model(provider_id=provider.id, native_model_id="blind")
    update_model(blind.id, {"capability_overrides": {"vision": "no"}})
    sighted = add_model(provider_id=provider.id, native_model_id="sighted")
    update_model(sighted.id, {"capability_overrides": {"vision": "yes"}})
    ranked = rank(Requirements(modalities_in=frozenset({Modality.IMAGE})), Preferences())
    assert [m.id for m in ranked] == [sighted.id]


def test_context_too_small_excludes(scratch, provider):
    model = add_model(provider_id=provider.id, native_model_id="small")
    from jarvis.model_system.registry import record_discovery
    record_discovery(model.id, {"contextWindow": 4000})
    ranked = rank(Requirements(min_context_tokens=100000), Preferences())
    assert ranked == []


def test_never_failed_outranks_a_recovered_but_previously_failed_model(scratch, provider):
    from jarvis.db import get_db

    a = add_model(provider_id=provider.id, native_model_id="a", quality=3)
    b = add_model(provider_id=provider.id, native_model_id="b", quality=3)
    record_failure(a.id, ErrorKind.PROVIDER_UNAVAILABLE)
    # Back-date the failure well past its cooldown — `a` is eligible again,
    # tied with `b` on every score input except its OWN history of having
    # failed at all. AVAILABILITY_BONUS exists to guarantee `b` still wins.
    get_db().execute("UPDATE ai_health SET since = '2000-01-01T00:00:00.000Z' WHERE model_id = ?",
                     (a.id,))
    assert [m.id for m in rank(Requirements(), Preferences())] == [b.id, a.id]


def test_preferred_model_moves_to_front_when_eligible(scratch, provider):
    a = add_model(provider_id=provider.id, native_model_id="a", quality=5)
    b = add_model(provider_id=provider.id, native_model_id="b", quality=1)
    ranked = rank(Requirements(), Preferences(preferred_model_id=b.id))
    assert ranked[0].id == b.id


def test_preferred_model_that_is_disabled_is_not_forced_back_in(scratch, provider):
    a = add_model(provider_id=provider.id, native_model_id="a")
    update_model(a.id, {"enabled": False})
    b = add_model(provider_id=provider.id, native_model_id="b")
    ranked = rank(Requirements(), Preferences(preferred_model_id=a.id))
    assert [m.id for m in ranked] == [b.id]


def test_control_role_prefers_vision(scratch, provider):
    blind = add_model(provider_id=provider.id, native_model_id="blind", quality=3)
    update_model(blind.id, {"capability_overrides": {"vision": "no"}})
    sighted = add_model(provider_id=provider.id, native_model_id="sighted", quality=3)
    update_model(sighted.id, {"capability_overrides": {"vision": "yes"}})
    ranked = rank(Requirements(), Preferences(role=Role.CONTROL))
    assert ranked[0].id == sighted.id


def test_cost_tier_falls_back_to_the_shared_price_ledger(scratch, provider):
    """The spend report's own ledger (seeded by cost/prices.py) is a real
    source of pricing the router should use when the model's own record
    carries none — one ledger, not two that could disagree."""
    from jarvis.cost import store as cost_store

    cheap = add_model(provider_id=provider.id, native_model_id="cheap", quality=3)
    pricey = add_model(provider_id=provider.id, native_model_id="pricey", quality=3)
    cost_store.set_price(provider=cheap.maker, model_id=cheap.native_model_id,
                         price_in=0.0000005, price_out=0.0000005)
    cost_store.set_price(provider=pricey.maker, model_id=pricey.native_model_id,
                         price_in=0.0001, price_out=0.0001)
    ranked = rank(Requirements(), Preferences(role=Role.BACKGROUND))
    # Background work weighs cost heavily (quality*2 - cost*2), so the cheaper
    # ledger-priced model must outrank the identically-configured pricier one.
    assert ranked[0].id == cheap.id


def test_select_returns_the_top_candidate_or_none(scratch, provider):
    assert select(Requirements(), Preferences()) is None
    add_model(provider_id=provider.id, native_model_id="only-one")
    assert select(Requirements(), Preferences()) is not None
