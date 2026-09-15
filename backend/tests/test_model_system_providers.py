import pytest

from jarvis.model_system.credentials import CredentialStatus
from jarvis.model_system.providers import (
    AuthMethod, BUILTIN_TEMPLATES, ProviderKind, add_provider, get_provider,
    get_template, list_providers, remove_provider, update_provider,
)


def test_builtin_templates_cover_the_expected_providers():
    ids = {t["id"] for t in BUILTIN_TEMPLATES}
    assert {"anthropic", "openai", "gemini", "local", "custom"} <= ids


def test_get_template_is_none_for_unknown():
    assert get_template("does-not-exist") is None


def test_add_provider_from_a_builtin_template(scratch):
    template = get_template("anthropic")
    provider = add_provider(
        label=template["label"], kind=template["kind"], adapter=template["adapter"],
        base_url=template["base_url"], auth_method=template["auth_method"],
        secret="sk-ant-test", key_required=template["key_required"],
        builtin=True, provider_id=template["id"],
    )
    assert provider.id == "anthropic"
    assert provider.kind is ProviderKind.NATIVE
    assert provider.credential_status is CredentialStatus.CONFIGURED
    # The secret itself must never appear on the public shape.
    d = provider.as_dict()
    assert "credential_ref" not in d
    assert "secret" not in d
    assert d["credentialStatus"] == "configured"


def test_custom_provider_is_the_same_table_no_special_casing(scratch):
    custom = add_provider(label="My Server", kind=ProviderKind.OPENAI_COMPATIBLE,
                          adapter="openai_compatible", base_url="https://example.test/v1",
                          auth_method=AuthMethod.API_KEY, secret="sk-custom")
    local = add_provider(label="Local Ollama", kind=ProviderKind.LOCAL,
                         adapter="openai_compatible", base_url="http://localhost:11434/v1",
                         auth_method=AuthMethod.NONE, key_required=False)
    assert type(custom) is type(local)
    assert local.credential_status is CredentialStatus.CONFIGURED  # no key required


def test_duplicate_provider_id_is_rejected(scratch):
    add_provider(label="One", kind=ProviderKind.OPENAI_COMPATIBLE, adapter="openai_compatible",
                provider_id="dupe")
    with pytest.raises(ValueError):
        add_provider(label="Two", kind=ProviderKind.OPENAI_COMPATIBLE,
                    adapter="openai_compatible", provider_id="dupe")


def test_reserved_ids_are_never_minted(scratch):
    provider = add_provider(label="discover", kind=ProviderKind.OPENAI_COMPATIBLE,
                            adapter="openai_compatible")
    assert provider.id != "discover"


def test_update_provider_cannot_change_adapter_or_kind(scratch):
    provider = add_provider(label="Original", kind=ProviderKind.OPENAI_COMPATIBLE,
                            adapter="openai_compatible")
    updated = update_provider(provider.id, {"adapter": "anthropic", "kind": "native",
                                            "label": "Renamed"})
    assert updated.adapter == "openai_compatible"
    assert updated.kind is ProviderKind.OPENAI_COMPATIBLE
    assert updated.label == "Renamed"


def test_update_provider_unknown_raises(scratch):
    with pytest.raises(KeyError):
        update_provider("does-not-exist", {"label": "x"})


def test_remove_provider_cascades_models(scratch):
    from jarvis.model_system.registry import add_model, get_model

    provider = add_provider(label="Removable", kind=ProviderKind.OPENAI_COMPATIBLE,
                            adapter="openai_compatible")
    model = add_model(provider_id=provider.id, native_model_id="some-model")
    removed = remove_provider(provider.id)
    assert removed == 1
    assert get_provider(provider.id) is None
    assert get_model(model.id) is None


def test_remove_unknown_provider_is_a_noop(scratch):
    assert remove_provider("never-existed") == 0


def test_list_providers_orders_by_creation(scratch):
    add_provider(label="First", kind=ProviderKind.OPENAI_COMPATIBLE, adapter="openai_compatible")
    add_provider(label="Second", kind=ProviderKind.OPENAI_COMPATIBLE, adapter="openai_compatible")
    labels = [p.label for p in list_providers()]
    assert labels == ["First", "Second"]
