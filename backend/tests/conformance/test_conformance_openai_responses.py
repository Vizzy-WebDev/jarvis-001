"""Conformance: OpenAI's own Responses API (`providers/openai_responses.py`)."""

from __future__ import annotations

import pytest

from conformance_harness import Case, check_case, check_minimal_stream, check_over_the_wire, check_shape, \
    check_tool_stream
from jarvis.models.providers import openai_responses as adapter

FORMAT = "openai-responses"
LIMITS = {"x-ratelimit-reset-requests": "20s", "x-ratelimit-remaining-requests": "0",
          "x-ratelimit-reset-tokens": "1s", "x-ratelimit-remaining-tokens": "5000"}

CASES = [
    Case("no quota left", 429, {"error": {"code": "insufficient_quota", "type": "insufficient_quota",
                                          "message": "You exceeded your current quota"}},
         kind="billing", scope="provider", retryable=False),
    Case("billing hard limit", 400, {"error": {"code": "billing_hard_limit_reached",
                                               "message": "Billing hard limit has been reached"}},
         kind="billing", scope="provider", retryable=False),
    Case("context too long", 400, {"error": {"code": "context_length_exceeded", "type": "invalid_request_error",
                                             "message": "maximum context length is 128000 tokens"}},
         kind="request", scope="model", retryable=False),
    Case("rate limited, reset headers", 429, {"error": {"code": "rate_limit_exceeded", "type": "requests",
                                                        "message": "Rate limit reached"}},
         kind="rate", scope="model", retryable=True, headers=LIMITS, retry_after_s=20.0),
    Case("rate limited, Retry-After", 429, {"error": {"code": "rate_limit_exceeded", "message": "slow"}},
         kind="rate", scope="model", retryable=True, headers={"Retry-After": "7"}, retry_after_s=7.0),
    Case("bad key", 401, {"error": {"code": "invalid_api_key", "type": "invalid_request_error",
                                    "message": "Incorrect API key provided"}},
         kind="auth", scope="credential", retryable=False),
    Case("no such model", 404, {"error": {"code": "model_not_found", "message": "The model `x` does not exist"}},
         kind="model", scope="model", retryable=False),
    Case("server error", 500, {"error": {"code": "server_error", "type": "server_error", "message": "oops"}},
         kind="server", scope="provider", retryable=True, headers={"Retry-After": "5"}, retry_after_s=5.0),
    Case("bad request", 400, {"error": {"type": "invalid_request_error", "message": "Unknown parameter"}},
         kind="request", scope="request", retryable=False),
    Case("in-stream server failure", None, {"error": {"code": "server_error", "message": "failed"}},
         kind="server", scope="provider", retryable=True),
]


def test_satisfies_the_adapter_protocol():
    check_shape(adapter)


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_normalizes_its_canonical_errors(case):
    check_case(adapter, case)


def test_a_minimal_request_streams_well_formed(stub_for):
    check_minimal_stream(adapter, stub_for(FORMAT))
    check_tool_stream(adapter, stub_for(FORMAT, tool=("get_time", {"zone": "UTC"})))


@pytest.mark.parametrize("case", [c for c in CASES if c.status], ids=[c.name for c in CASES if c.status])
def test_the_same_failure_over_a_real_socket_reaches_the_same_verdict(stub_for, case):
    check_over_the_wire(adapter, stub_for(FORMAT), case)


def test_an_in_stream_failure_event_is_normalized_too(stub_for):
    from jarvis.models.errors import ProviderError
    from conformance_harness import minimal_request
    from jarvis.models.types import Target

    stub = stub_for(FORMAT, stream_error={"code": "rate_limit_exceeded", "message": "slow down"})
    with pytest.raises(ProviderError) as caught:
        list(adapter.stream(Target(stub.base_url, "k"), minimal_request()))
    assert (caught.value.kind, caught.value.scope, caught.value.retryable_elsewhere) == ("rate", "model", True)
