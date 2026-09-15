import pytest

from jarvis.model_system.reasoning import (
    Effort, NO_REASONING, ReasoningKind, ReasoningScheme, UNKNOWN_SCHEME,
    clamp, plan, scheme_from_dict,
)


def test_scheme_with_no_levels_is_fine_for_none_and_unknown():
    assert ReasoningScheme(kind=ReasoningKind.NONE).levels == ()
    assert UNKNOWN_SCHEME.kind is ReasoningKind.UNKNOWN


def test_tiers_scheme_rejects_empty_levels():
    with pytest.raises(ValueError):
        ReasoningScheme(kind=ReasoningKind.TIERS)


def test_tiers_scheme_rejects_unordered_levels():
    with pytest.raises(ValueError):
        ReasoningScheme(kind=ReasoningKind.TIERS, levels=(Effort.HIGH, Effort.LOW),
                        default=Effort.LOW, native={Effort.LOW: "low", Effort.HIGH: "high"})


def test_tiers_scheme_requires_default_among_levels():
    with pytest.raises(ValueError):
        ReasoningScheme(kind=ReasoningKind.TIERS, levels=(Effort.LOW,),
                        default=Effort.HIGH, native={Effort.LOW: "low"})


def test_clamp_goes_down_never_up():
    scheme = ReasoningScheme(kind=ReasoningKind.TIERS, levels=(Effort.LOW, Effort.HIGH),
                             default=Effort.LOW, native={Effort.LOW: "low", Effort.HIGH: "high"})
    level, clamped = clamp(Effort.MAXIMUM, scheme)
    assert level is Effort.HIGH
    assert clamped is True


def test_clamp_below_floor_lands_on_floor():
    scheme = ReasoningScheme(kind=ReasoningKind.TIERS, levels=(Effort.MEDIUM, Effort.HIGH),
                             default=Effort.MEDIUM, native={Effort.MEDIUM: "m", Effort.HIGH: "h"})
    level, clamped = clamp(Effort.OFF, scheme)
    assert level is Effort.MEDIUM
    assert clamped is True


def test_clamp_exact_match_is_not_a_clamp():
    scheme = ReasoningScheme(kind=ReasoningKind.TIERS, levels=(Effort.LOW, Effort.HIGH),
                             default=Effort.LOW, native={Effort.LOW: "low", Effort.HIGH: "high"})
    level, clamped = clamp(Effort.HIGH, scheme)
    assert level is Effort.HIGH
    assert clamped is False


def test_clamp_on_none_scheme_sends_nothing():
    assert clamp(Effort.HIGH, NO_REASONING) == (None, False)


def test_plan_uses_scheme_default_when_nothing_requested():
    scheme = ReasoningScheme(kind=ReasoningKind.TIERS, levels=(Effort.LOW, Effort.HIGH),
                             default=Effort.LOW, native={Effort.LOW: "low", Effort.HIGH: "high"})
    result = plan(None, scheme)
    assert result is not None
    assert result.level is Effort.LOW
    assert result.clamped is False


def test_plan_on_uncontrollable_scheme_is_none():
    assert plan(Effort.HIGH, NO_REASONING) is None
    assert plan(Effort.HIGH, UNKNOWN_SCHEME) is None


def test_plan_reports_the_clamp():
    scheme = ReasoningScheme(kind=ReasoningKind.TIERS, levels=(Effort.LOW,),
                             default=Effort.LOW, native={Effort.LOW: "low"})
    result = plan(Effort.MAXIMUM, scheme)
    assert result.requested is Effort.MAXIMUM
    assert result.level is Effort.LOW
    assert result.clamped is True
    assert result.native == "low"


def test_scheme_round_trips_through_as_dict():
    scheme = ReasoningScheme(kind=ReasoningKind.BUDGET, levels=(Effort.LOW, Effort.HIGH),
                             default=Effort.LOW, native={Effort.LOW: 1024, Effort.HIGH: 32768})
    restored = scheme_from_dict(scheme.as_dict())
    assert restored is not None
    assert restored.kind is ReasoningKind.BUDGET
    assert restored.levels == (Effort.LOW, Effort.HIGH)
    assert restored.native[Effort.HIGH] == 32768


def test_scheme_from_dict_never_raises_on_garbage():
    assert scheme_from_dict("not a dict") is None
    assert scheme_from_dict({"kind": "not-a-real-kind"}) is None
    assert scheme_from_dict({"kind": "tiers", "native": {}}) is None
    assert scheme_from_dict(None) is None


def test_scheme_from_dict_none_and_unknown_round_trip():
    assert scheme_from_dict(NO_REASONING.as_dict()).kind is ReasoningKind.NONE
