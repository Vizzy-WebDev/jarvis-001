"""Which generation parameters a model actually takes, and building a request
that only sends those.

**Never send every parameter to every provider.** A parameter a model does not
take is not harmless to include — some providers reject the whole request,
some silently ignore it, and a control that looks like it works but is quietly
dropped is worse than no control at all, because nobody can tell the
difference from outside. So support here is tracked per parameter, per model,
the same three-state way `model_system/capabilities.py` tracks a capability: `YES`,
`NO`, and `UNKNOWN` are different facts, and only a confirmed `NO` — from a
provider's own supported-parameter listing, or from a live request it actually
rejected — ever removes a control from a request.

`UNKNOWN` is offered rather than withheld, on the same reasoning the router
uses for a capability nobody has asked about: trying is the only way to find
out, and the cost of being wrong is one rejected request, which
`model_system/fallback.py` turns into a remembered fact rather than a repeated failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from .capabilities import Support


class Param(Enum):
    """One normalized generation parameter. The adapter for a given wire
    format is the only place that knows what each becomes on the wire."""

    TEMPERATURE = "temperature"
    TOP_P = "top_p"
    TOP_K = "top_k"
    MAX_OUTPUT_TOKENS = "max_output_tokens"
    STOP_SEQUENCES = "stop_sequences"
    FREQUENCY_PENALTY = "frequency_penalty"
    PRESENCE_PENALTY = "presence_penalty"
    SEED = "seed"
    TOOL_CHOICE = "tool_choice"
    PARALLEL_TOOL_CALLS = "parallel_tool_calls"
    #: Structured/JSON-schema output configuration — see `model_system/request.py`'s
    #: `ResponseFormat`. Its own parameter, not folded into tool calling: a
    #: model can support one without the other.
    RESPONSE_FORMAT = "response_format"


@dataclass(frozen=True)
class GenerationParams:
    """What a caller is asking for, in normalized terms. Any field left `None`
    (or empty, for the sequence) means "let the model use its own default" —
    never a value this system invented on the caller's behalf."""

    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    max_output_tokens: int | None = None
    stop_sequences: tuple[str, ...] = ()
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    seed: int | None = None
    tool_choice: Any = None
    parallel_tool_calls: bool | None = None
    response_format: Mapping[str, Any] | None = None

    def value_of(self, param: Param) -> Any:
        value = getattr(self, param.value, None)
        if param is Param.STOP_SEQUENCES:
            return value if value else None
        return value


_FIELD_TO_PARAM = {p.value: p for p in Param}


def params_from_dict(value: Mapping[str, Any] | None) -> dict[Param, Support]:
    """A parameter-support map from stored JSON — a model's declared or
    discovered supported-parameter list. Never raises."""
    data = value or {}
    out: dict[Param, Support] = {}
    for name, raw in data.items():
        param = _FIELD_TO_PARAM.get(str(name))
        if param is None:
            continue
        support = raw if isinstance(raw, Support) else None
        if support is None and isinstance(raw, bool):
            support = Support.YES if raw else Support.NO
        if support is None and isinstance(raw, str):
            try:
                support = Support(raw.lower())
            except ValueError:
                support = None
        if support is not None:
            out[param] = support
    return out


def merge_param_support(*sources: Mapping[Param, Support] | None) -> dict[Param, Support]:
    """Per-parameter precedence, first source wins — same shape as
    `capabilities.merge_capabilities`. Callers pass highest precedence first:
    learned refusal, then user override, then discovered, then catalog seed."""
    merged: dict[Param, Support] = {}
    for source in sources:
        if not source:
            continue
        for param, support in source.items():
            if param not in merged and support is not Support.UNKNOWN:
                merged[param] = support
    return merged


def build_params(requested: GenerationParams, supported: Mapping[Param, Support]) -> dict[str, Any]:
    """Only the requested values a model has not been confirmed to reject.

    A parameter the caller did not ask for is never invented here. A
    parameter the caller asked for but this model has confirmed `NO` on is
    dropped silently — the caller gets the rest of its request rather than a
    hard failure over one setting nothing promised would be honoured.
    """
    out: dict[str, Any] = {}
    for param in Param:
        value = requested.value_of(param)
        if value is None:
            continue
        if supported.get(param, Support.UNKNOWN) is Support.NO:
            continue
        out[param.value] = value
    return out


def rejected_params(configured: GenerationParams, sent: Mapping[str, Any]) -> frozenset[Param]:
    """Which of the caller's requested parameters `build_params` actually left
    out — for a caller (a routes layer) that wants to report a configuration
    it could not honour rather than pretend everything was sent."""
    left_out: set[Param] = set()
    for param in Param:
        if configured.value_of(param) is not None and param.value not in sent:
            left_out.add(param)
    return frozenset(left_out)
