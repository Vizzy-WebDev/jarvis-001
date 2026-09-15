"""The openai_compatible adapter's own surface — discovery and connection
testing — over the real stub HTTP server."""

from __future__ import annotations

import pytest

from jarvis.model_system.adapters import openai_compatible
from jarvis.model_system.providers import AuthMethod, ProviderKind, add_provider

from stub_openai_server import StubModelServer


@pytest.fixture
def stub():
    server = StubModelServer()
    server.base_url = server.start()
    yield server
    server.stop()


def _provider(scratch, stub, **kw):
    return add_provider(label="Stub", kind=ProviderKind.OPENAI_COMPATIBLE,
                        adapter="openai_compatible", base_url=stub.base_url,
                        auth_method=AuthMethod.NONE, key_required=False, **kw)


def test_test_connection_proves_generation_not_just_listing(scratch, stub):
    stub.says("ready")
    provider = _provider(scratch, stub)
    result = openai_compatible.test_connection(provider, "stub-model")
    assert result["ok"] is True


def test_test_connection_reports_a_real_failure(scratch, stub):
    stub.fails(500, "server is on fire")
    provider = _provider(scratch, stub)
    result = openai_compatible.test_connection(provider, "stub-model")
    assert result["ok"] is False
    assert "server is on fire" in result["error"]


def test_test_connection_empty_reply_is_not_ok(scratch, stub):
    stub.says("")
    provider = _provider(scratch, stub)
    result = openai_compatible.test_connection(provider, "stub-model")
    assert result["ok"] is False


def test_discover_models_reads_the_extended_fields(scratch, stub):
    stub.models = [{"id": "stub-model", "context_length": 128000,
                    "supported_parameters": ["reasoning_effort", "tools"]}]
    provider = _provider(scratch, stub)
    listed = openai_compatible.discover_models(provider)
    assert listed[0]["model"] == "stub-model"
    assert listed[0]["contextTokens"] == 128000
    assert listed[0]["supported_parameters"] == ["reasoning_effort", "tools"]


def test_missing_key_when_required_raises_before_any_request(scratch, stub):
    provider = add_provider(label="NeedsKey", kind=ProviderKind.OPENAI_COMPATIBLE,
                            adapter="openai_compatible", base_url=stub.base_url,
                            auth_method=AuthMethod.API_KEY, key_required=True)
    with pytest.raises(openai_compatible.NoCredential):
        openai_compatible._client(provider)
