"""The probe cascade for an address the user just typed, against a real
OpenAI-shaped HTTP stub — the shape almost every custom/local server speaks.

Two permissions are checked, not one: `discover_models()` proves the address
can be LISTED, `test_connection()` proves it can actually GENERATE. A probe
that only did the first would save a key that 401s on the very first real
turn.
"""

from __future__ import annotations

from jarvis.model_system.probe import _kind_for, normalize_base_url, probe_endpoint

from stub_openai_server import StubModelServer


def test_an_empty_address_is_rejected_before_any_network_call(scratch):
    result = probe_endpoint("   ")
    assert result.ok is False
    assert result.error == "Please enter a server address."


def test_normalize_base_url_strips_trailing_slash_and_a_pasted_completions_path():
    assert normalize_base_url("http://localhost:1234/v1/chat/completions/") == "http://localhost:1234/v1"
    assert normalize_base_url(" http://localhost:1234/v1/ ") == "http://localhost:1234/v1"


def test_kind_for_classifies_loopback_and_private_hosts_as_local():
    assert _kind_for("http://127.0.0.1:8000/v1") == "local"
    assert _kind_for("http://192.168.1.20:8000/v1") == "local"
    assert _kind_for("https://openrouter.ai/api/v1") == "aggregator"


def test_a_keyless_local_server_is_confirmed_end_to_end(scratch):
    stub = StubModelServer()
    base = stub.start()
    try:
        stub.says("ready")  # consumed by the one real generation call
        result = probe_endpoint(base)

        assert result.ok is True
        assert result.adapter == "openai_compatible"
        assert result.base_url == base
        assert result.kind == "local"
        assert result.key_required is False
        assert result.needs_key is False
        assert [m["model"] for m in result.models] == ["stub-model"]
        assert any("This connection works" in step for step in result.steps)
    finally:
        stub.stop()


def test_a_keyed_server_is_confirmed_with_the_supplied_key(scratch):
    stub = StubModelServer()
    base = stub.start()
    try:
        stub.says("ready")
        result = probe_endpoint(base, secret="sk-test-value")

        assert result.ok is True
        assert result.key_required is True
        # The staged transient credential really rode the outbound request.
        assert stub.requests[-1]["auth"] == "Bearer sk-test-value"
    finally:
        stub.stop()


def test_an_auth_wall_with_no_key_supplied_reports_needs_a_key(scratch):
    stub = StubModelServer()
    base = stub.start()
    try:
        stub.fails(401, "missing key")
        result = probe_endpoint(base)

        assert result.ok is False
        assert result.needs_key is True
        assert result.adapter == "openai_compatible"
        assert "needs an API key" in result.error
    finally:
        stub.stop()


def test_an_auth_wall_rejects_the_supplied_key(scratch):
    stub = StubModelServer()
    base = stub.start()
    try:
        stub.fails(401, "bad key")
        result = probe_endpoint(base, secret="sk-wrong")

        assert result.ok is False
        assert result.needs_key is False
        assert "rejected that key" in result.error
    finally:
        stub.stop()


def test_listing_works_but_generation_fails_is_reported_as_a_failure(scratch):
    """The stub can only script one HTTP call at a time in sequence, so this
    drives `_confirm_generation` directly on top of a listing that already
    succeeded — the exact split `probe_endpoint` itself makes internally, and
    the reason two permissions (list vs. generate) are checked instead of one."""
    from jarvis.model_system.adapters import openai_compatible
    from jarvis.model_system.probe import ProbeResult, _confirm_generation

    stub = StubModelServer()
    base = stub.start()
    try:
        stub.fails(500, "upstream broke")
        result = ProbeResult(ok=False)
        outcome = _confirm_generation(openai_compatible, base, None, None,
                                      [{"model": "stub-model"}], result)

        assert outcome is not None
        assert outcome.ok is False
        assert "upstream broke" in outcome.error
        assert outcome.adapter == "openai_compatible"
    finally:
        stub.stop()


def test_an_unreachable_address_exhausts_the_cascade_without_raising(scratch):
    result = probe_endpoint("http://127.0.0.1:1")

    assert result.ok is False
    assert result.needs_key is False
    assert result.error == "I couldn't work out how to talk to that address."


def test_a_cancelled_probe_leaves_no_trace_in_saved_credentials(scratch):
    from jarvis.config import get_secret

    stub = StubModelServer()
    base = stub.start()
    try:
        stub.says("ready")
        probe_endpoint(base, secret="sk-should-not-be-saved")
        # probe_endpoint stages then discards its own transient ref; nothing
        # about that secret is ever written to .env.
        assert get_secret("sk-should-not-be-saved") is None
    finally:
        stub.stop()
