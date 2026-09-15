"""ai/fallback.py: retry, then the next candidate, recorded and bounded —
exercised over real HTTP against a stub server so the failures are real
provider failures, not simulated ones."""

from __future__ import annotations

import pytest

from jarvis.model_system.fallback import NoModelAvailable, execute
from jarvis.model_system.health import is_eligible, status_of
from jarvis.model_system.providers import AuthMethod, ProviderKind, add_provider
from jarvis.model_system.registry import add_model
from jarvis.model_system.request import AIRequest, Completed, Message, ModelSwitched, TextDelta

from stub_openai_server import StubModelServer


@pytest.fixture
def stub_a():
    server = StubModelServer()
    server.base_url = server.start()
    yield server
    server.stop()


@pytest.fixture
def stub_b():
    server = StubModelServer()
    server.base_url = server.start()
    yield server
    server.stop()


def _provider_and_model(stub, label):
    provider = add_provider(label=label, kind=ProviderKind.OPENAI_COMPATIBLE,
                            adapter="openai_compatible", base_url=stub.base_url,
                            auth_method=AuthMethod.NONE, key_required=False)
    return add_model(provider_id=provider.id, native_model_id="stub-model", quality=3)


def test_a_working_model_answers_with_no_switch(scratch, stub_a):
    stub_a.says("hello")
    model = _provider_and_model(stub_a, "only")
    request = AIRequest(messages=(Message(role="user", text="hi"),), model_id=model.id)
    events = list(execute(request))
    assert not any(isinstance(e, ModelSwitched) for e in events)
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "hello"


def test_falls_over_to_the_next_candidate_on_a_model_level_failure(scratch, stub_a, stub_b):
    stub_a.fails(401, "bad key")  # a real, non-retryable, model-level failure
    stub_b.says("from b")
    model_a = _provider_and_model(stub_a, "a")
    model_b = _provider_and_model(stub_b, "b")
    # Pinned to `a` explicitly so which one fails first is deterministic.
    request = AIRequest(messages=(Message(role="user", text="hi"),), model_id=model_a.id)
    events = list(execute(request))
    switches = [e for e in events if isinstance(e, ModelSwitched)]
    assert len(switches) == 1
    assert switches[0].to_model_id == model_b.id
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "from b"
    # The failed model is benched; the one that answered is recorded healthy
    # (a real row, not absence — §13 asks for "last successful request" to
    # be tracked, which needs a row to track it on).
    assert is_eligible(model_a.id) is False
    assert status_of(model_b.id)["state"] == "healthy"


def test_auth_failure_benches_the_model(scratch, stub_a):
    stub_a.fails(401, "bad key")
    model = _provider_and_model(stub_a, "only")
    request = AIRequest(messages=(Message(role="user", text="hi"),), model_id=model.id)
    with pytest.raises(NoModelAvailable):
        list(execute(request))
    assert is_eligible(model.id) is False


def test_a_retryable_failure_is_retried_before_falling_over(scratch, stub_a):
    stub_a.fails(429, "slow down").fails(429, "slow down again").says("finally")
    model = _provider_and_model(stub_a, "only")
    request = AIRequest(messages=(Message(role="user", text="hi"),), model_id=model.id)
    events = list(execute(request))
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "finally"
    assert not any(isinstance(e, ModelSwitched) for e in events)  # same candidate throughout


def test_retries_are_bounded_then_give_up_on_this_candidate(scratch, stub_a, stub_b):
    stub_a.fails(429, "one").fails(429, "two").fails(429, "three")
    stub_b.says("from b")
    model_a = _provider_and_model(stub_a, "a")
    model_b = _provider_and_model(stub_b, "b")
    request = AIRequest(messages=(Message(role="user", text="hi"),), model_id=model_a.id)
    events = list(execute(request))
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "from b"


def test_no_candidates_raises_with_a_real_explanation(scratch):
    request = AIRequest(messages=(Message(role="user", text="hi"),))
    with pytest.raises(NoModelAvailable, match="no models set up"):
        list(execute(request))


def test_a_successful_call_feeds_the_shared_cost_ledger(scratch, stub_a):
    """The unified spend report (LLM + TTS + STT) reads `cost_events`, fed by
    `observers/cost.py` subscribing to MODEL_CALL_COMPLETED — this system
    must keep publishing that event in the vocabulary it already reads.

    Isolated on its OWN fresh `EventBus` rather than the process-wide shared
    one: another test elsewhere in the suite may have started the real
    observers on the shared bus and never torn them down, and this test's own
    subscription would then double-count alongside them.
    """
    from jarvis.cost import store as cost_store
    from jarvis.events.bus import EventBus, EventType
    from jarvis.observers.cost import record_model_call

    stub_a.says("hi")
    model = _provider_and_model(stub_a, "only")
    isolated_bus = EventBus()
    isolated_bus.subscribe(EventType.MODEL_CALL_COMPLETED, record_model_call)
    request = AIRequest(messages=(Message(role="user", text="hi"),), model_id=model.id)
    list(execute(request, event_bus=isolated_bus))
    events = cost_store.list_events_for_model(model.maker, model.native_model_id)
    assert len(events) == 1
    assert events[0]["unitsIn"] == 11


def test_content_policy_failure_does_not_bench_the_model(scratch, stub_a):
    stub_a.fails(400, "blocked by safety system")
    model = _provider_and_model(stub_a, "only")
    request = AIRequest(messages=(Message(role="user", text="hi"),), model_id=model.id)
    with pytest.raises(NoModelAvailable):
        list(execute(request))
    # A content-policy failure is a fact about the REQUEST, not the model.
    assert is_eligible(model.id) is True
