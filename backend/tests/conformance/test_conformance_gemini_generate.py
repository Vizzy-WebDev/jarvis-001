"""Conformance: Google's Gemini generateContent API (`providers/gemini_generate.py`)."""

from __future__ import annotations

import pytest

from conformance_harness import Case, check_case, check_minimal_stream, check_over_the_wire, check_shape, \
    check_tool_stream, minimal_request
from jarvis.models.errors import ProviderError
from jarvis.models.providers import gemini_generate as adapter
from jarvis.models.types import Target

FORMAT = "gemini-generatecontent"
RETRY = {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37s"}
BAD_KEY = {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID",
           "domain": "googleapis.com"}


def body(code: int, status: str, message: str, *details: dict) -> dict:
    return {"error": {"code": code, "message": message, "status": status, "details": list(details)}}


CASES = [
    Case("quota exhausted with retryDelay", 429, body(429, "RESOURCE_EXHAUSTED", "You exceeded your quota", RETRY),
         kind="rate", scope="model", retryable=True, retry_after_s=37.0),
    Case("permission denied", 403, body(403, "PERMISSION_DENIED", "Permission denied on resource"),
         kind="auth", scope="credential", retryable=False),
    Case("invalid argument", 400, body(400, "INVALID_ARGUMENT", "Invalid JSON payload received"),
         kind="request", scope="request", retryable=False),
    Case("bad key, sent as 400", 400, body(400, "INVALID_ARGUMENT", "API key not valid.", BAD_KEY),
         kind="auth", scope="credential", retryable=False),
    Case("too many input tokens for this model", 400,
         body(400, "INVALID_ARGUMENT", "The input token count (1200000) exceeds the maximum number of tokens"),
         kind="request", scope="model", retryable=False),
    Case("unavailable", 503, body(503, "UNAVAILABLE", "The model is overloaded. Please try again later."),
         kind="overloaded", scope="provider", retryable=True),
    Case("internal", 500, body(500, "INTERNAL", "An internal error has occurred"),
         kind="server", scope="provider", retryable=True),
    Case("no such model", 404, body(404, "NOT_FOUND", "models/nope is not found"),
         kind="model", scope="model", retryable=False),
    Case("billing / region precondition", 400, body(400, "FAILED_PRECONDITION", "User location is not supported"),
         kind="billing", scope="credential", retryable=False),
    Case("in-stream quota frame", None, body(429, "RESOURCE_EXHAUSTED", "quota", RETRY),
         kind="rate", scope="model", retryable=True, retry_after_s=37.0),
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


def test_an_in_stream_error_frame_is_normalized_too(stub_for):
    stub = stub_for(FORMAT, stream_error=body(429, "RESOURCE_EXHAUSTED", "quota", RETRY)["error"])
    with pytest.raises(ProviderError) as caught:
        list(adapter.stream(Target(stub.base_url, "k"), minimal_request()))
    assert (caught.value.kind, caught.value.scope, caught.value.retry_after_s, caught.value.status) == (
        "rate", "model", 37.0, 429)
