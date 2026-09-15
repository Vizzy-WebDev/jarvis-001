import pytest

from jarvis.model_system.errors import ErrorKind
from jarvis.model_system.health import (
    HealthState, clear, is_eligible, record_failure, record_success, retry_after_ms, status_of,
)
from jarvis.model_system.providers import ProviderKind, add_provider
from jarvis.model_system.registry import add_model


@pytest.fixture
def two_models(scratch):
    """Health is keyed on the MODEL row, not any shared string — these two
    represent the same underlying model reached through two providers, which
    is exactly the case a rate limit on one route must never affect the other."""
    p1 = add_provider(label="Direct", kind=ProviderKind.NATIVE, adapter="openai_compatible")
    p2 = add_provider(label="Reseller", kind=ProviderKind.AGGREGATOR, adapter="openai_compatible")
    a = add_model(provider_id=p1.id, native_model_id="gpt-5.6-luna")
    b = add_model(provider_id=p2.id, native_model_id="gpt-5.6-luna")
    return a, b


def test_never_failed_is_eligible(scratch, two_models):
    a, _ = two_models
    assert is_eligible(a.id) is True
    assert status_of(a.id) is None


def test_a_failure_is_recorded_and_makes_it_ineligible(scratch, two_models):
    a, _ = two_models
    record_failure(a.id, ErrorKind.RATE_LIMIT, detail="slow down")
    assert is_eligible(a.id) is False
    status = status_of(a.id)
    assert status["state"] == HealthState.RATE_LIMITED.value
    assert status["failureCount"] == 1


def test_repeated_failures_increment_the_count(scratch, two_models):
    a, _ = two_models
    record_failure(a.id, ErrorKind.RATE_LIMIT)
    record_failure(a.id, ErrorKind.RATE_LIMIT)
    assert status_of(a.id)["failureCount"] == 2


def test_success_clears_the_bad_state_and_resets_the_count(scratch, two_models):
    a, _ = two_models
    record_failure(a.id, ErrorKind.RATE_LIMIT)
    record_success(a.id)
    assert is_eligible(a.id) is True
    status = status_of(a.id)
    assert status["state"] == "healthy"
    assert status["failureCount"] == 0


def test_different_states_get_different_cooldowns(scratch, two_models):
    a, b = two_models
    record_failure(a.id, ErrorKind.RATE_LIMIT)            # 30 min
    record_failure(b.id, ErrorKind.PROVIDER_UNAVAILABLE)  # 2 min
    assert retry_after_ms(a.id) > retry_after_ms(b.id)


def test_auth_error_keeps_a_model_out_for_hours(scratch, two_models):
    a, _ = two_models
    record_failure(a.id, ErrorKind.AUTHENTICATION)
    assert retry_after_ms(a.id) > 60_000  # well over a minute


def test_clear_removes_the_record(scratch, two_models):
    a, _ = two_models
    record_failure(a.id, ErrorKind.RATE_LIMIT)
    clear(a.id)
    assert status_of(a.id) is None
    assert is_eligible(a.id) is True


def test_two_routes_of_the_same_model_are_tracked_independently(scratch, two_models):
    a, b = two_models
    record_failure(a.id, ErrorKind.RATE_LIMIT)
    assert is_eligible(a.id) is False
    assert is_eligible(b.id) is True


def test_deleting_the_model_removes_its_health_record(scratch, two_models):
    from jarvis.model_system.registry import delete_model

    a, _ = two_models
    record_failure(a.id, ErrorKind.RATE_LIMIT)
    delete_model(a.id)
    assert status_of(a.id) is None
