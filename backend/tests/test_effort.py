"""Reasoning effort: resolving it, sending it, and surviving a refusal.

The tests this file exists for are the benching ones. Before this work, an
error saying a parameter was not recognised had no classification of its own,
so every one of them benched a healthy model for having been asked a question
it did not understand — and, with no memory of the refusal, did it again on the
next turn, and the next.

The exact penalty was measured against the old classifier rather than assumed,
because the first draft of this comment overstated it. Most phrasings
("Unrecognized request argument…", "Extra inputs are not permitted…", 'Unknown
name "thinking_config"') fell through to `other`, which is a 20-minute
cooldown. One shape — a message carrying "invalid request" or "invalid
argument" alongside the field name — matched `_INVALID_ARGUMENT_TEXT` and
landed on `unsupported`, which is six hours. Those numbers are recorded in the
comment on `REFUSAL_MESSAGES` below rather than re-measured by a test: a test
that reads the old code out of git compares against a moving target, and began
comparing the new classifier with itself as soon as this work was committed.

Twenty minutes is not six hours, and it is still wrong: the model is fine, the
request was the problem, and the next request would not have reproduced it.

The refusal path is exercised against the real HTTP stub rather than a mock,
because what is being proved is that a 400 from a server turns into a second
request with one field removed — and a mock of the SDK would prove only that
the test author believed it would.
"""

from __future__ import annotations

import time

import pytest

from jarvis.adapters import anthropic_adapter, gemini_adapter, openai_compatible
from jarvis.adapters.base import model_for
from jarvis.catalog import Effort, EffortKind, EffortRequest, EffortScheme
from jarvis.gateway import effort as effort_module
from jarvis.gateway.effort import call_with_effort, clamp, plan
from jarvis.gateway.error_kind import (
    availability_state_for, benches_the_model, classify_error, refused_parameter,
)

TIERS = EffortScheme(
    kind=EffortKind.TIERS,
    levels=(Effort.OFF, Effort.LOW, Effort.MEDIUM, Effort.HIGH, Effort.MAX),
    default=Effort.MEDIUM,
    native={Effort.OFF: "none", Effort.LOW: "low", Effort.MEDIUM: "medium",
            Effort.HIGH: "high", Effort.MAX: "max"},
)

#: A deliberately gappy ladder — the shape that makes clamping a real decision
#: rather than a bounds check.
SPARSE = EffortScheme(
    kind=EffortKind.TIERS, levels=(Effort.LOW, Effort.HIGH), default=Effort.LOW,
    native={Effort.LOW: "low", Effort.HIGH: "high"},
)

BUDGET = EffortScheme(
    kind=EffortKind.BUDGET,
    levels=(Effort.OFF, Effort.LOW, Effort.HIGH),
    default=Effort.LOW,
    native={Effort.OFF: 0, Effort.LOW: 4096, Effort.HIGH: 32768},
)

NONE = EffortScheme(kind=EffortKind.NONE)
UNKNOWN = EffortScheme(kind=EffortKind.UNKNOWN)


@pytest.fixture(autouse=True)
def _isolated_effort_store(scratch):
    """Every test gets its own learned-refusal file; none touch real data."""
    effort_module.reset_for_tests()
    yield
    effort_module.reset_for_tests()


def _request(level: Effort, scheme: EffortScheme) -> EffortRequest:
    return EffortRequest(level=level, scheme=scheme, requested=level, clamped=False)


# --- the hazard ------------------------------------------------------------

class _Refusal(Exception):
    """Shaped like a provider's 400 about an unknown field."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = 400


def test_a_refused_parameter_is_not_treated_as_a_broken_model():
    """A model that merely did not recognise a field is not an unusable model,
    and must not be recorded as one at any cooldown length."""
    err = _Refusal("Unrecognized request argument supplied: reasoning_effort")

    assert classify_error(err) == "parameter_unsupported"
    assert classify_error(err) != "unsupported"
    assert benches_the_model(err) is False


#: Real refusal phrasings, one per wire format, plus the one that used to be
#: expensive. Each is a message a provider actually sends when it does not
#: recognise a field.
#:
#: The historical cost of each was measured against the pre-change classifier
#: while this work was done: the first three fell through to `other`, a
#: twenty-minute cooldown, and the last matched `_INVALID_ARGUMENT_TEXT` and
#: drew the six-hour `unsupported` state. Those numbers are recorded here rather
#: than re-derived by a test, because a test that reads the previous version out
#: of git anchors itself to a moving target — the first draft of this file did
#: exactly that, and started comparing the new classifier against itself the
#: moment the change was committed.
REFUSAL_MESSAGES = (
    "Unrecognized request argument supplied: reasoning_effort",
    "Extra inputs are not permitted: thinking",
    'Invalid JSON payload received. Unknown name "thinking_config"',
    "thinking: invalid request, no such field",
)


@pytest.mark.parametrize("message", REFUSAL_MESSAGES)
def test_no_refusal_of_one_of_our_parameters_ever_benches_the_model(message):
    """The invariant, stated forwards rather than as a historical comparison.

    Whatever a provider's phrasing, a complaint about a field we sent is a fact
    about the request. The last message is the one that used to cost six hours,
    and the one the first version of the fix still missed.
    """
    err = _Refusal(message)

    assert classify_error(err) == "parameter_unsupported", message
    assert benches_the_model(err) is False, message
    assert availability_state_for(err) != "unsupported", message


def test_even_if_something_records_it_anyway_it_is_not_the_harshest_cooldown():
    """Defence in depth: callers should not record this at all, but if one
    does, the fallback must not be the six-hour state."""
    err = _Refusal("Unknown parameter: 'thinking'")

    assert availability_state_for(err) == "error"
    assert availability_state_for(err) != "unsupported"


def test_a_genuinely_unusable_model_is_still_benched():
    """The other half. If the new classification swallowed real failures, a
    dead model would stay in the rotation being retried forever.

    The last two matter most: `_PARAMETER_REFUSAL_TEXT` deliberately includes
    the generic "invalid request"/"invalid argument" phrasings, so these are
    messages that DO match a refusal phrase and must still be benched — they
    are kept apart only by naming no parameter we send.
    """
    for message in ("not a valid model", "model not found", "unknown model",
                    "invalid request: model not found",
                    "invalid argument: no allowed providers"):
        err = _Refusal(message)
        assert classify_error(err) == "unsupported", message
        assert benches_the_model(err) is True, message


def test_a_refusal_must_name_a_parameter_we_actually_send():
    """The safety property that keeps the two apart.

    "is not supported" appears in plenty of errors that mean the MODEL is
    unusable. Only a message that ALSO names a field we sent counts.
    """
    assert refused_parameter(_Refusal("this model is not supported")) is None
    assert refused_parameter(_Refusal("thinking is not supported")) == "thinking"
    # A field name with no refusal phrasing is not a refusal either.
    assert refused_parameter(_Refusal("thinking produced 5 tokens")) is None


@pytest.mark.parametrize("message,expected", [
    ("Unrecognized request argument supplied: reasoning_effort", "reasoning_effort"),
    ("Extra inputs are not permitted: thinking", "thinking"),
    ('Invalid JSON payload received. Unknown name "thinking_config"', "thinking_config"),
    ("unsupported parameter: thinking_budget", "thinking_budget"),
])
def test_each_wire_format_says_it_differently_and_all_are_recognised(message, expected):
    assert refused_parameter(_Refusal(message)) == expected


# --- clamping --------------------------------------------------------------

def test_a_level_the_version_offers_passes_through_unchanged():
    assert clamp(Effort.MEDIUM, TIERS) == (Effort.MEDIUM, False)


def test_a_request_above_the_ceiling_comes_down_to_it():
    assert clamp(Effort.MAX, SPARSE) == (Effort.HIGH, True)


def test_a_gap_in_the_ladder_resolves_DOWNWARD():
    """Never spend more than was asked for.

    Given (low, high) and a request for medium, the answer is low. The opposite
    rule would quietly bill more than the person chose, on a key they pay for.
    """
    assert clamp(Effort.MEDIUM, SPARSE) == (Effort.LOW, True)


def test_a_request_below_everything_on_offer_lands_on_the_floor():
    """There is nothing lower to give, and refusing the turn over it would be
    absurd."""
    assert clamp(Effort.OFF, SPARSE) == (Effort.LOW, True)


def test_a_version_with_no_reasoning_control_resolves_to_nothing_to_send():
    assert clamp(Effort.HIGH, NONE) == (None, False)
    assert clamp(Effort.HIGH, UNKNOWN) == (None, False)


def test_planning_falls_back_to_the_versions_own_default():
    planned = plan(None, TIERS, provider="p", model="m")

    assert planned is not None
    assert planned.level is Effort.MEDIUM
    assert planned.clamped is False


def test_planning_reports_that_it_clamped():
    """A clamp nobody can see is indistinguishable from the setting being
    ignored."""
    planned = plan(Effort.MAX, SPARSE, provider="p", model="m")

    assert planned is not None
    assert planned.level is Effort.HIGH
    assert planned.requested is Effort.MAX
    assert planned.clamped is True


# --- remembering a refusal -------------------------------------------------

def test_a_recorded_refusal_stops_the_parameter_being_sent_again():
    assert plan(Effort.HIGH, TIERS, provider="p", model="m") is not None

    effort_module.mark_unsupported("p", "m", detail="Unknown parameter")

    assert plan(Effort.HIGH, TIERS, provider="p", model="m") is None
    assert effort_module.is_unsupported("p", "m") is True


def test_a_refusal_is_remembered_per_version_not_across_the_roster():
    effort_module.mark_unsupported("p", "m")

    assert effort_module.is_unsupported("p", "other-model") is False
    assert effort_module.is_unsupported("other-provider", "m") is False


def test_a_refusal_survives_a_restart():
    """The whole point of a file rather than memory: re-learning this costs a
    failed request every time."""
    effort_module.mark_unsupported("p", "m")
    effort_module.reset_for_tests()

    assert effort_module.is_unsupported("p", "m") is True


def test_a_refusal_can_be_cleared():
    effort_module.mark_unsupported("p", "m")
    effort_module.clear("p", "m")

    assert effort_module.is_unsupported("p", "m") is False


def test_a_recorded_refusal_never_stores_a_credential():
    """This file is written from a provider's own error text, and a provider
    can quote the key it was sent."""
    effort_module.mark_unsupported(
        "p", "m", detail="rejected key sk-not-a-real-key-000000000000 for reasoning_effort")

    stored = effort_module._load()["p/m"]["detail"]
    assert "sk-not-a-real-key-000000000000" not in (stored or "")


# --- the retry -------------------------------------------------------------

class _FakeAdapter:
    """Records what it was asked for; fails the first call on demand."""

    def __init__(self, fail_first: Exception | None = None, text_before_failure: str = ""):
        self.fail_first = fail_first
        self.text_before_failure = text_before_failure
        self.calls: list[EffortRequest | None] = []

    def stream(self, entry, messages, *, system="", tools=None, effort=None):
        self.calls.append(effort)
        if self.fail_first is not None:
            error, self.fail_first = self.fail_first, None
            if self.text_before_failure:
                yield self.text_before_failure
            raise error
        yield "ok"


def test_a_refusal_retries_the_same_model_without_the_parameter():
    adapter = _FakeAdapter(_Refusal("Unknown parameter: 'reasoning_effort'"))
    asked = _request(Effort.HIGH, TIERS)

    events = list(call_with_effort(adapter, {"model": "m"}, [], effort=asked,
                                   provider="p", model="m"))

    assert events == ["ok"]
    assert adapter.calls == [asked, None], "the retry must drop the parameter, not the model"
    assert effort_module.is_unsupported("p", "m") is True


def test_a_failure_that_is_not_about_the_parameter_is_not_retried():
    adapter = _FakeAdapter(_Refusal("rate limit exceeded"))

    with pytest.raises(Exception):
        list(call_with_effort(adapter, {"model": "m"}, [], effort=_request(Effort.HIGH, TIERS),
                              provider="p", model="m"))

    assert len(adapter.calls) == 1
    assert effort_module.is_unsupported("p", "m") is False


def test_nothing_is_retried_when_no_parameter_was_sent():
    adapter = _FakeAdapter(_Refusal("Unknown parameter: 'reasoning_effort'"))

    with pytest.raises(Exception):
        list(call_with_effort(adapter, {"model": "m"}, [], effort=None,
                              provider="p", model="m"))

    assert len(adapter.calls) == 1


def test_a_refusal_after_text_has_streamed_is_not_retried():
    """Guarded rather than assumed. A rejected parameter is a 400 before any
    tokens exist, so this should never happen — but if it did, retrying would
    show the user the first part of the answer twice.
    """
    adapter = _FakeAdapter(_Refusal("Unknown parameter: 'reasoning_effort'"),
                           text_before_failure="half an answer")

    with pytest.raises(Exception):
        list(call_with_effort(adapter, {"model": "m"}, [], effort=_request(Effort.HIGH, TIERS),
                              provider="p", model="m"))

    assert len(adapter.calls) == 1


# --- what actually reaches each wire ---------------------------------------

def test_the_openai_shaped_wire_sends_reasoning_effort():
    kwargs = openai_compatible._reasoning_kwargs(_request(Effort.HIGH, TIERS))
    assert kwargs == {"reasoning_effort": "high"}


def test_the_openai_shaped_wire_skips_a_budget_it_cannot_express():
    """Skipped rather than approximated: this endpoint has no field for a token
    budget, and inventing one produces a rejection that reads like a broken
    model."""
    assert openai_compatible._reasoning_kwargs(_request(Effort.HIGH, BUDGET)) == {}


def test_anthropics_wire_sends_a_thinking_budget():
    assert anthropic_adapter._thinking(_request(Effort.HIGH, BUDGET)) == {
        "type": "enabled", "budget_tokens": 32768}


def test_anthropics_wire_sends_no_thinking_block_at_all_for_off():
    """A budget of zero is not a request any provider accepts, and is not what
    the person meant."""
    assert anthropic_adapter._thinking(_request(Effort.OFF, BUDGET)) is None


def test_anthropics_wire_skips_a_tier_it_cannot_express():
    assert anthropic_adapter._thinking(_request(Effort.HIGH, TIERS)) is None


def test_geminis_wire_carries_both_shapes():
    assert gemini_adapter._thinking_config(_request(Effort.HIGH, TIERS)) == {
        "thinkingLevel": "high"}
    assert gemini_adapter._thinking_config(_request(Effort.HIGH, BUDGET)) == {
        "thinkingBudget": 32768}
    assert gemini_adapter._thinking_config(_request(Effort.OFF, BUDGET)) is None


def test_nothing_is_sent_when_there_is_no_effort_to_send():
    assert openai_compatible._reasoning_kwargs(None) == {}
    assert anthropic_adapter._thinking(None) is None
    assert gemini_adapter._thinking_config(None) is None


def test_a_variant_scheme_changes_which_model_is_called():
    """Some providers express reasoning by publishing a separate model rather
    than by taking a parameter. Wire-agnostic, so it is handled once."""
    variant = EffortScheme(
        kind=EffortKind.VARIANT, levels=(Effort.LOW, Effort.HIGH), default=Effort.LOW,
        native={Effort.LOW: "base-model", Effort.HIGH: "base-model-thinking"},
    )

    assert model_for({"model": "base-model"}, _request(Effort.HIGH, variant)) \
        == "base-model-thinking"
    assert model_for({"model": "base-model"}, _request(Effort.HIGH, TIERS)) == "base-model"
    assert model_for({"model": "base-model"}, None) == "base-model"


# --- end to end, through a real server --------------------------------------

def test_a_real_400_becomes_a_second_request_with_the_field_removed(scratch):
    """The whole mechanism, against a real HTTP server rather than a fake.

    A mock would only prove the test author believed a 400 turns into a retry.
    This drives the actual adapter against an actual socket, and then reads the
    two request bodies the server received.
    """
    from stub_openai_server import StubModelServer

    server = StubModelServer()
    base_url = server.start()
    try:
        server.fails(400, "Unrecognized request argument supplied: reasoning_effort")
        server.says("the answer")

        entry = {"model": "stub-model", "baseUrl": base_url, "keyRequired": False}
        events = list(call_with_effort(
            openai_compatible, entry, [{"role": "user", "text": "hi"}],
            effort=_request(Effort.HIGH, TIERS), provider="stub", model="stub-model"))
    finally:
        server.stop()

    assert len(server.requests) == 2, "one rejected call, one retry"
    assert server.requests[0]["body"].get("reasoning_effort") == "high"
    assert "reasoning_effort" not in server.requests[1]["body"], \
        "the retry must not carry the field the server just refused"
    assert server.requests[1]["body"]["model"] == "stub-model", \
        "the model itself must be unchanged — only the parameter is dropped"
    assert any(getattr(e, "text", "") == "the answer" for e in events)
    assert effort_module.is_unsupported("stub", "stub-model") is True


def test_the_second_turn_does_not_pay_for_the_refusal_again(scratch):
    """What the persisted record is FOR. Without it every turn would spend a
    failed round trip, forever, on rosters that are already rate-limited."""
    from stub_openai_server import StubModelServer

    server = StubModelServer()
    base_url = server.start()
    try:
        server.fails(400, "Unrecognized request argument supplied: reasoning_effort")
        server.says("first")
        server.says("second")

        entry = {"model": "stub-model", "baseUrl": base_url, "keyRequired": False}
        for _ in range(2):
            planned = plan(Effort.HIGH, TIERS, provider="stub", model="stub-model")
            list(call_with_effort(openai_compatible, entry, [{"role": "user", "text": "hi"}],
                                  effort=planned, provider="stub", model="stub-model"))
    finally:
        server.stop()

    assert len(server.requests) == 3, "two calls on the first turn, one on the second"
    assert "reasoning_effort" not in server.requests[2]["body"]


def test_a_refused_first_attempt_is_not_timed_as_the_model_being_slow():
    """The retry restarts the clock, and the caller cannot do that itself.

    Measured from outside `call_with_effort`, a refused first attempt would be
    added to the successful second one and recorded as latency — so a model
    that answered promptly on the retry would be ranked slow for it, on exactly
    the one turn where the refusal is discovered. The clock lives here because
    this is the only layer that knows a retry happened.
    """
    readings: list[float] = []

    class RefusesOnce:
        def __init__(self):
            self.calls = 0

        def stream(self, entry, messages, *, system="", tools=None, effort=None):
            self.calls += 1
            if effort is not None:
                time.sleep(0.05)
                raise RuntimeError("Unrecognized request argument supplied: reasoning_effort")
            yield "answered"

    adapter = RefusesOnce()
    events = list(call_with_effort(
        adapter, {"model": "m"}, [],
        effort=_request(Effort.HIGH, TIERS),
        provider="p", model="m", on_first_token=readings.append))

    assert events == ["answered"]
    assert adapter.calls == 2
    assert len(readings) == 1, "only the attempt that produced something is timed"
    assert readings[0] < 50, "the refused attempt's 50ms is not counted"
