"""Recognising which configured service belongs to which voice provider.

The generic key store lets someone type ANY label — "ElevenLabs", "Elevenlab",
"11labs" — and the ref is whatever that slugifies to. There is no guaranteed
exact match between that and what an adapter expects to find itself under.

**A plain substring check was tried first and failed live, twice, on two real
typos.** "elevenlab" (no trailing s) needed the check widened once already, and
"elevenlap" (a b→p slip) does not contain "elevenlab" as a substring AT ALL, so
it silently matched nothing: the service vanished from the voice picker and the
Test button with no error, because nothing was wrong from either side's point of
view — they simply never found each other. A heuristic that has already failed
live more than once needs to close the whole failure CLASS, not have one more
special case bolted on. So this is real typo tolerance, not a longer list.
"""

from __future__ import annotations

import re

#: Roughly one or two character slips on a ten-character name.
MAX_TYPO_DISTANCE = 2


def normalise(ref: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", str(ref or "").lower())


def _distance(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, ch_a in enumerate(a, start=1):
        current = [i]
        for j, ch_b in enumerate(b, start=1):
            current.append(previous[j - 1] if ch_a == ch_b
                           else 1 + min(previous[j], current[j - 1], previous[j - 1]))
        previous = current
    return previous[-1]


def looks_like(ref: str | None, canonical_names: list[str]) -> bool:
    text = normalise(ref)
    # Too short to fuzzy-match without genuinely risking someone else's ref.
    if len(text) < 4:
        return False
    for name in canonical_names:
        if text in name or name in text:
            return True
        # Lengths too far apart for an edit distance of two to mean anything —
        # it would either always fail or start matching unrelated names.
        if abs(len(text) - len(name)) > MAX_TYPO_DISTANCE + 1:
            continue
        if _distance(text, name) <= MAX_TYPO_DISTANCE:
            return True
    return False
