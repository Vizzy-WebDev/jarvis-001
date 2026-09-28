"""Conformance: Anthropic's Messages API (`providers/anthropic_messages.py`)."""

from __future__ import annotations

import pytest

from conformance_harness import Case, check_case, check_minimal_stream, check_over_the_wire, check_shape, \
    check_tool_stream, minimal_request
from jarvis.models.errors import ProviderError
from jarvis.models.providers import anthropic_messages as adapter
from jarvis.models.types import Target

FORMAT = "anthropic-messages"


def body(kind: str, message: str) -> dict:
    return {"type": "error", "error": {"type": kind, "message": message}}


CASES = [
    Case("overloaded", 529, body("overloaded_error", "Overloaded"),
         kind="overloaded", scope="provider", retryable=True),
    Case("529 with no body", 529, "overloaded", kind="overloaded", scope="provider", retryable=True),
    Case("rate limited", 429, body("rate_limit_error", "Number of requests has exceeded your rate limit"),
         kind="rate", scope="model", retryable=True, headers={"retry-after": "12"}, retry_after_s=12.0),
    Case("bad key", 401, body("authentication_error", "invalid x-api-key"),
         kind="auth", scope="credential", retryable=False),
    Case("no permission", 403, body("permission_error", "Your API key does not have permission"),
         kind="forbidden", scope="credential", retryable=False),
    Case("bad request", 400, body("invalid_request_error", "messages: field required"),
         kind="request", scope="request", retryable=False),
    Case("prompt too long for this model", 400, body("invalid_request_error", "prompt is too long: 250000 tokens"),
         kind="request", scope="model", retryable=False),
    Case("no such model", 404, body("not_found_error", "model: claude-nope"),
         kind="model", scope="model", retryable=False),
    Case("API error", 500, body("api_error", "Internal server error"),
         kind="server", scope="provider", retryable=True),
    Case("out of credit, sent as a bad request", 400,
         body("invalid_request_error", "Your credit balance is too low to access the Anthropic API."),
         kind="billing", scope="credential", retryable=False),
    Case("billing", 400, body("billing_error", "Your credit balance is too low"),
         kind="billing", scope="credential", retryable=False),
    Case("in-stream overloaded event", None, body("overloaded_error", "Overloaded"),
         kind="overloaded", scope="provider", retryable=True),
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


def test_an_in_stream_error_event_is_normalized_too(stub_for):
    stub = stub_for(FORMAT, stream_error={"type": "overloaded_error", "message": "Overloaded"})
    with pytest.raises(ProviderError) as caught:
        list(adapter.stream(Target(stub.base_url, "k"), minimal_request()))
    assert (caught.value.kind, caught.value.scope, caught.value.retryable_elsewhere) == (
        "overloaded", "provider", True)
    assert "Overloaded" in str(caught.value)
