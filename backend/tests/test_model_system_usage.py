import pytest

from jarvis.model_system.providers import AuthMethod, ProviderKind, add_provider
from jarvis.model_system.registry import add_model
from jarvis.model_system.request import Usage
from jarvis.model_system.usage import for_request, record, speed_tier, summary_since


@pytest.fixture
def model(scratch):
    provider = add_provider(label="Test", kind=ProviderKind.OPENAI_COMPATIBLE,
                            adapter="openai_compatible", auth_method=AuthMethod.NONE,
                            key_required=False)
    return add_model(provider_id=provider.id, native_model_id="m1")


def test_record_and_read_back(scratch, model):
    record(request_id="r1", model_id=model.id, provider_id=model.provider.id, role="conversation",
          success=True, usage=Usage(tokens_in=10, tokens_out=5), ttft_ms=200, latency_ms=900)
    rows = for_request("r1")
    assert len(rows) == 1
    assert rows[0]["tokensIn"] == 10
    assert rows[0]["success"] is True


def test_a_number_never_measured_is_absent_not_zero(scratch, model):
    record(request_id="r1", model_id=model.id, provider_id=model.provider.id, role="conversation",
          success=True)
    row = for_request("r1")[0]
    assert row["tokensIn"] is None
    assert row["ttftMs"] is None


def test_speed_tier_is_none_until_measured(scratch, model):
    assert speed_tier(model.id) is None


def test_speed_tier_reflects_recent_ttft(scratch, model):
    for _ in range(5):
        record(request_id="r", model_id=model.id, provider_id=model.provider.id,
              role="conversation", success=True, ttft_ms=200)
    assert speed_tier(model.id) == 5  # fast


def test_speed_tier_ignores_failed_attempts(scratch, model):
    record(request_id="r", model_id=model.id, provider_id=model.provider.id,
          role="conversation", success=False, ttft_ms=50, error_type="timeout")
    assert speed_tier(model.id) is None


def test_fallback_chain_round_trips(scratch, model):
    record(request_id="r1", model_id=model.id, provider_id=model.provider.id, role="conversation",
          success=True, fallback_chain=[{"modelId": "other-model"}])
    row = for_request("r1")[0]
    assert row["fallbackChain"] == [{"modelId": "other-model"}]


def test_summary_since_groups_by_provider_and_model(scratch, model):
    record(request_id="r1", model_id=model.id, provider_id=model.provider.id, role="conversation",
          success=True, usage=Usage(tokens_in=10, tokens_out=5), cost_estimate=0.01)
    record(request_id="r2", model_id=model.id, provider_id=model.provider.id, role="conversation",
          success=True, usage=Usage(tokens_in=20, tokens_out=10), cost_estimate=0.02)
    summary = summary_since("2000-01-01T00:00:00.000Z")
    assert len(summary) == 1
    assert summary[0]["calls"] == 2
    assert summary[0]["tokensIn"] == 30
    assert summary[0]["cost"] == pytest.approx(0.03)
