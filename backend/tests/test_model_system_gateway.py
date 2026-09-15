"""The gateway's single-model execution path, exercised over real HTTP against
a stub server — the adapter under test is the REAL adapter, so SSE framing,
the empty-`choices` usage chunk, and tool-call deltas are genuinely parsed."""

from __future__ import annotations

import pytest

from jarvis.model_system.errors import ErrorKind
from jarvis.model_system.gateway import ModelNotFound, execute, run
from jarvis.model_system.providers import AuthMethod, ProviderKind, add_provider
from jarvis.model_system.registry import add_model
from jarvis.model_system.request import (
    AIRequest, Completed, ErrorEvent, Message, TextDelta, ToolDefinition,
)

from stub_openai_server import StubModelServer


@pytest.fixture
def stub():
    server = StubModelServer()
    server.base_url = server.start()
    yield server
    server.stop()


def _provider_and_model(scratch, stub, *, key_required=False):
    provider = add_provider(label="Stub", kind=ProviderKind.OPENAI_COMPATIBLE,
                            adapter="openai_compatible", base_url=stub.base_url,
                            auth_method=AuthMethod.NONE if not key_required else AuthMethod.API_KEY,
                            key_required=key_required,
                            secret="sk-test" if key_required else None)
    model = add_model(provider_id=provider.id, native_model_id="stub-model")
    return provider, model


def test_run_returns_the_full_text(scratch, stub):
    stub.says("hello there")
    _, model = _provider_and_model(scratch, stub)
    request = AIRequest(messages=(Message(role="user", text="hi"),), model_id=model.id, stream=False)
    response = run(request)
    assert response.text == "hello there"
    assert response.finish_reason == "stop"
    assert response.usage.tokens_in == 11


def test_execute_streams_text_deltas_then_one_completed(scratch, stub):
    stub.says("abcdef")
    _, model = _provider_and_model(scratch, stub)
    request = AIRequest(messages=(Message(role="user", text="hi"),), model_id=model.id)
    events = list(execute(request))
    deltas = [e for e in events if isinstance(e, TextDelta)]
    completed = [e for e in events if isinstance(e, Completed)]
    assert "".join(d.text for d in deltas) == "abcdef"
    assert len(completed) == 1
    assert completed[0].finish_reason == "stop"


def test_tool_call_round_trips(scratch, stub):
    stub.calls_tool("search", {"q": "weather"})
    _, model = _provider_and_model(scratch, stub)
    request = AIRequest(
        messages=(Message(role="user", text="what's the weather"),),
        tools=(ToolDefinition(name="search", description="search the web",
                              parameters={"type": "object", "properties": {}}),),
        model_id=model.id,
    )
    response = run(request)
    assert response.finish_reason == "tool_calls"
    assert response.tool_calls[0].name == "search"
    assert response.tool_calls[0].args == {"q": "weather"}


def test_error_payload_in_a_200_is_raised_not_streamed(scratch, stub):
    stub.says('{"error":{"message":"[429] upstream rate limited"}}')
    _, model = _provider_and_model(scratch, stub)
    request = AIRequest(messages=(Message(role="user", text="hi"),), model_id=model.id)
    with pytest.raises(Exception, match="upstream rate limited"):
        list(execute(request))


def test_a_real_failure_yields_an_error_event_before_raising(scratch, stub):
    stub.fails(429, "slow down")
    _, model = _provider_and_model(scratch, stub)
    request = AIRequest(messages=(Message(role="user", text="hi"),), model_id=model.id)
    events = []
    with pytest.raises(Exception):
        for event in execute(request):
            events.append(event)
    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(errors) == 1
    assert errors[0].kind == ErrorKind.RATE_LIMIT.value


def test_unknown_model_id_raises_model_not_found(scratch, stub):
    request = AIRequest(messages=(Message(role="user", text="hi"),), model_id="does-not-exist")
    with pytest.raises(ModelNotFound):
        list(execute(request))


def test_no_model_id_raises_model_not_found(scratch, stub):
    """Auto is the router's job (ai/router.py, stage 3) — execute() alone
    never guesses a model."""
    request = AIRequest(messages=(Message(role="user", text="hi"),))
    with pytest.raises(ModelNotFound):
        list(execute(request))


def test_missing_credential_is_a_clean_failure_not_a_crash(scratch, stub):
    provider = add_provider(label="NeedsKey", kind=ProviderKind.OPENAI_COMPATIBLE,
                            adapter="openai_compatible", base_url=stub.base_url,
                            auth_method=AuthMethod.API_KEY, key_required=True)
    model = add_model(provider_id=provider.id, native_model_id="stub-model")
    request = AIRequest(messages=(Message(role="user", text="hi"),), model_id=model.id)
    with pytest.raises(Exception, match="API key"):
        list(execute(request))
