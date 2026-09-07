"""Reading the usage numbers a provider already sent back.

Every SDK reports what a call consumed, and every adapter in the Node original
threw it away — which is why "how much have I spent" could only ever be
estimated. This module is the one place each provider's own shape is translated
into the neutral `{"unitsIn", "unitsOut", "cachedIn"}` the cost store records.

**A number that was not reported is absent, never zero.** Zero is a claim ("this
call cost nothing"); absence is the truth ("the provider did not say"). The
whole subsystem's honesty rests on not blurring those two, so `_clean()` drops
any key whose value is not a real number rather than defaulting it.

Pure and dependency-free: no SDK import, no I/O. It reads whatever attributes
happen to be on the object it is handed, so a provider adding or renaming a
field degrades to "not reported" instead of raising inside a live turn.
"""

from __future__ import annotations

from typing import Any


def _num(source: Any, *names: str) -> int | None:
    """The first of `names` present on `source` as a real integer, else None."""
    for name in names:
        value = getattr(source, name, None)
        if value is None and isinstance(source, dict):
            value = source.get(name)
        if isinstance(value, bool):      # a bool is an int in Python; never a count
            continue
        if isinstance(value, (int, float)):
            return int(value)
    return None


def _clean(**fields: int | None) -> dict[str, int] | None:
    kept = {key: value for key, value in fields.items() if isinstance(value, int)}
    return kept or None


def from_openai(usage: Any) -> dict[str, int] | None:
    """OpenAI-shaped: the final, empty-`choices` chunk's `usage` object."""
    if usage is None:
        return None
    details = getattr(usage, "prompt_tokens_details", None)
    return _clean(
        unitsIn=_num(usage, "prompt_tokens", "input_tokens"),
        unitsOut=_num(usage, "completion_tokens", "output_tokens"),
        cachedIn=_num(details, "cached_tokens") if details is not None else None,
    )


def from_anthropic(usage: Any) -> dict[str, int] | None:
    """Anthropic-shaped: `final_message.usage`."""
    if usage is None:
        return None
    return _clean(
        unitsIn=_num(usage, "input_tokens"),
        unitsOut=_num(usage, "output_tokens"),
        cachedIn=_num(usage, "cache_read_input_tokens"),
    )


def from_gemini(metadata: Any) -> dict[str, int] | None:
    """Gemini-shaped: a chunk's `usage_metadata`.

    Gemini reports CUMULATIVE totals for the turn on each chunk, so an adapter
    keeps the last one it saw rather than summing — summing would multiply one
    turn's real cost by the number of chunks it happened to arrive in.
    """
    if metadata is None:
        return None
    return _clean(
        unitsIn=_num(metadata, "prompt_token_count"),
        unitsOut=_num(metadata, "candidates_token_count"),
        cachedIn=_num(metadata, "cached_content_token_count"),
    )
