import pytest

from jarvis.model_system.parameters import GenerationParams
from jarvis.model_system.profiles import (
    add_profile, apply_profile, delete_profile, get_default_profile, get_profile,
    list_profiles, resolve_request, set_default_profile, update_profile,
)
from jarvis.model_system.providers import AuthMethod, ProviderKind, add_provider
from jarvis.model_system.reasoning import Effort
from jarvis.model_system.registry import add_model
from jarvis.model_system.request import AIRequest, Message


@pytest.fixture
def model(scratch):
    provider = add_provider(label="Test", kind=ProviderKind.OPENAI_COMPATIBLE,
                            adapter="openai_compatible", auth_method=AuthMethod.NONE,
                            key_required=False)
    return add_model(provider_id=provider.id, native_model_id="m1")


def test_builtin_profiles_are_seeded_on_first_read(scratch):
    seeded = list_profiles()
    ids = {p.id for p in seeded}
    assert {"default", "fast", "deep-reasoning", "coding"} <= ids


def test_exactly_one_profile_is_default(scratch):
    defaults = [p for p in list_profiles() if p.is_default]
    assert len(defaults) == 1
    assert defaults[0].id == "default"


def test_default_profile_with_no_model_id_is_auto(scratch):
    default = get_default_profile()
    assert default.model_id is None  # Auto: the router decides every time


def test_add_a_custom_profile_naming_a_default_model_is_the_default_case(scratch, model):
    profile = add_profile(label="My Coding Setup", model_id=model.id, reasoning_level=Effort.HIGH)
    assert profile.model_id == model.id  # Default: a person chose this model
    assert profile.reasoning_level is Effort.HIGH


def test_set_default_profile_moves_the_flag_not_copies_it(scratch, model):
    custom = add_profile(label="Mine", model_id=model.id)
    set_default_profile(custom.id)
    assert get_default_profile().id == custom.id
    assert get_profile("default").is_default is False


def test_default_profile_cannot_be_deleted(scratch):
    with pytest.raises(ValueError):
        delete_profile("default")


def test_deleting_a_non_default_profile_works(scratch):
    custom = add_profile(label="Temp")
    delete_profile(custom.id)
    assert get_profile(custom.id) is None


def test_update_profile_changes_only_named_fields(scratch, model):
    custom = add_profile(label="Mine", model_id=model.id)
    updated = update_profile(custom.id, {"label": "Renamed"})
    assert updated.label == "Renamed"
    assert updated.model_id == model.id


def test_apply_profile_fills_gaps_never_overrides_an_explicit_choice(scratch, model):
    profile = add_profile(label="Deep", model_id=model.id, reasoning_level=Effort.HIGH)
    bare = AIRequest(messages=(Message(role="user", text="hi"),))
    filled = apply_profile(profile, bare)
    assert filled.model_id == model.id
    assert filled.reasoning is Effort.HIGH

    explicit = AIRequest(messages=(Message(role="user", text="hi"),), model_id="caller-chose-this",
                         reasoning=Effort.LOW)
    unchanged = apply_profile(profile, explicit)
    assert unchanged.model_id == "caller-chose-this"
    assert unchanged.reasoning is Effort.LOW


def test_apply_profile_merges_generation_params_leaving_explicit_ones_alone(scratch):
    profile = add_profile(label="P", params=GenerationParams(temperature=0.2, top_p=0.9))
    request = AIRequest(messages=(Message(role="user", text="hi"),),
                        params=GenerationParams(temperature=0.9))
    merged = apply_profile(profile, request)
    assert merged.params.temperature == 0.9   # caller's own choice wins
    assert merged.params.top_p == 0.9          # gap filled from the profile


def test_resolve_request_uses_the_named_profile(scratch, model):
    profile = add_profile(label="Named", model_id=model.id)
    request = AIRequest(messages=(Message(role="user", text="hi"),), profile_id=profile.id)
    resolved = resolve_request(request)
    assert resolved.model_id == model.id


def test_resolve_request_falls_back_to_the_default_profile(scratch, model):
    set_default_profile(add_profile(label="NewDefault", model_id=model.id).id)
    request = AIRequest(messages=(Message(role="user", text="hi"),))
    resolved = resolve_request(request)
    assert resolved.model_id == model.id


def test_a_fresh_install_resolves_to_pure_auto(scratch):
    """Nobody has configured a profile beyond the seeded default, whose
    model_id is None — a request must come back with no pin at all, so the
    router alone decides."""
    request = AIRequest(messages=(Message(role="user", text="hi"),))
    resolved = resolve_request(request)
    assert resolved.model_id is None
