"""Conformance: OpenAI-compatible Chat Completions (`providers/openai_chat.py`) — Ollama,
LM Studio, custom servers and gateways such as OpenRouter and OmniRoute."""

from __future__ import annotations

import pytest

from conformance_harness import Case, check_case, check_minimal_stream, check_over_the_wire, check_shape, \
    check_tool_stream, minimal_request
from jarvis.models.errors import ProviderError
from jarvis.models.providers import openai_chat as adapter
from jarvis.models.types import Target

FORMAT = "openai-chat"

CASES = [
    Case("rate limited", 429, {"error": {"code": "rate_limit_exceeded", "message": "Rate limit reached"}},
         kind="rate", scope="model", retryable=True, headers={"Retry-After": "3"}, retry_after_s=3.0),
    Case("429 that is about money", 429, {"error": {"message": "Rate limit: add credits to continue"}},
         kind="billing", scope="credential", retryable=False),
    Case("insufficient quota", 429, {"error": {"code": "insufficient_quota", "message": "quota"}},
         kind="billing", scope="provider", retryable=False),
    Case("bad key", 401, {"error": {"message": "No auth credentials found"}},
         kind="auth", scope="credential", retryable=False),
    Case("OpenRouter: needs credit", 402, {"error": {"code": 402, "message": "Insufficient credits"}},
         kind="billing", scope="credential", retryable=False),
    Case("gateway upstream failed (structured)", 502, {"error": {"code": 502, "message": "Provider returned error"}},
         kind="server", scope="model", retryable=True),
    Case("proxy is down (unparseable)", 503, "<html><body>503 Service Unavailable</body></html>",
         kind="server", scope="provider", retryable=True),
    Case("opaque 400 from a gateway", 400, {"error": {"message": "Provider returned error"}},
         kind="request", scope="unknown", retryable=False),
    Case("context too long", 400, {"error": {"code": "context_length_exceeded", "message": "too long"}},
         kind="request", scope="model", retryable=False),
    Case("no such model", 404, {"error": {"message": "model 'x' not found"}},
         kind="model", scope="model", retryable=False),
    Case("in-stream error, no code", None, {"error": {"message": "upstream unavailable"}},
         kind="server", scope="model", retryable=False),
    Case("in-stream error carrying OpenRouter's numeric code", None,
         {"error": {"code": 429, "message": "Rate limit exceeded: free-models-per-min"}},
         kind="rate", scope="model", retryable=True),
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
    stub = stub_for(FORMAT, stream_error={"code": 429, "message": "slow down"})
    with pytest.raises(ProviderError) as caught:
        list(adapter.stream(Target(stub.base_url, "k"), minimal_request()))
    assert (caught.value.kind, caught.value.scope, caught.value.retryable_elsewhere) == ("rate", "model", True)


def test_discovery_reports_ids_only_and_hands_the_row_on(stub_for):
    stub = stub_for(FORMAT, models=[{"id": "x", "owned_by": "combo", "pricing": {"prompt": "-1"}}])
    [found] = adapter.discover(Target(stub.base_url, "k"))
    assert found.model_id == "x" and found.facts is None and found.raw["owned_by"] == "combo"
