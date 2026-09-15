from jarvis.model_system.credentials import CredentialStatus, resolve, set_credential, status_of


def test_not_configured_when_nothing_saved(scratch):
    assert status_of("some_ref") is CredentialStatus.NOT_CONFIGURED


def test_configured_after_saving(scratch):
    set_credential("test_provider", "sk-real-secret-value")
    assert status_of("test_provider") is CredentialStatus.CONFIGURED


def test_key_not_required_is_always_configured(scratch):
    assert status_of(None, key_required=False) is CredentialStatus.CONFIGURED
    assert status_of("never-saved", key_required=False) is CredentialStatus.CONFIGURED


def test_unestablished_key_requirement_defaults_to_requiring_one(scratch):
    assert status_of("nothing-here", key_required=None) is CredentialStatus.NOT_CONFIGURED


def test_resolve_returns_the_real_value_for_adapter_use_only(scratch):
    set_credential("adapter_only", "the-actual-key")
    assert resolve("adapter_only") == "the-actual-key"
    assert resolve(None) is None


def test_clear_removes_it(scratch):
    from jarvis.model_system.credentials import clear_credential

    set_credential("to_clear", "value")
    clear_credential("to_clear")
    assert status_of("to_clear") is CredentialStatus.NOT_CONFIGURED
