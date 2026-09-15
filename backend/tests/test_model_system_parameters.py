from jarvis.model_system.capabilities import Support
from jarvis.model_system.parameters import (
    GenerationParams, Param, build_params, merge_param_support, params_from_dict,
    rejected_params,
)


def test_build_params_only_includes_what_was_requested():
    params = GenerationParams(temperature=0.7)
    sent = build_params(params, {})
    assert sent == {"temperature": 0.7}


def test_build_params_omits_a_confirmed_unsupported_param():
    params = GenerationParams(temperature=0.7, seed=42)
    sent = build_params(params, {Param.SEED: Support.NO})
    assert "seed" not in sent
    assert sent["temperature"] == 0.7


def test_build_params_sends_unknown_optimistically():
    """Only a confirmed NO withholds a parameter — UNKNOWN is offered and
    allowed to fail honestly, same rule as capability routing."""
    params = GenerationParams(top_k=40)
    sent = build_params(params, {Param.TOP_K: Support.UNKNOWN})
    assert sent["top_k"] == 40


def test_build_params_never_invents_a_value():
    sent = build_params(GenerationParams(), {})
    assert sent == {}


def test_empty_stop_sequences_is_not_sent():
    sent = build_params(GenerationParams(stop_sequences=()), {})
    assert "stop_sequences" not in sent


def test_rejected_params_reports_what_was_left_out():
    params = GenerationParams(temperature=0.5, seed=1)
    sent = build_params(params, {Param.SEED: Support.NO})
    assert rejected_params(params, sent) == frozenset({Param.SEED})


def test_params_from_dict_reads_strings_and_bools():
    parsed = params_from_dict({"seed": True, "top_k": "no", "temperature": "unknown"})
    assert parsed[Param.SEED] is Support.YES
    assert parsed[Param.TOP_K] is Support.NO
    assert parsed[Param.TEMPERATURE] is Support.UNKNOWN


def test_params_from_dict_ignores_unknown_keys():
    assert params_from_dict({"not_a_real_param": True}) == {}


def test_merge_param_support_first_wins():
    learned = {Param.SEED: Support.NO}
    discovered = {Param.SEED: Support.YES}
    merged = merge_param_support(learned, discovered)
    assert merged[Param.SEED] is Support.NO


def test_merge_param_support_skips_unknown_sources():
    merged = merge_param_support(None, {Param.SEED: Support.UNKNOWN}, {Param.SEED: Support.YES})
    assert merged[Param.SEED] is Support.YES
