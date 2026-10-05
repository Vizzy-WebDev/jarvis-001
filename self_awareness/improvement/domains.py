"""Subjects Jarvis must never proactively reason about.

Applied on BOTH sides of any pattern-finding call: matching material is filtered
out of the model's input, and any produced insight that still matches is dropped
after. Two chances to catch it, because the harm this prevents is content the
user excluded reaching a model call at all.

**Deliberately biased broad**, which is the opposite asymmetry from the tone
floors elsewhere: there, a false positive costs a slightly-too-careful reply;
here, a false NEGATIVE means someone's emotional life or relationships were fed
into an inference they never asked for. Over-excluding costs a missed pattern,
which is nothing.
"""

from __future__ import annotations

import re

EXCLUDED = re.compile(
    r"\b("
    r"depress|anxiet|anxious|lonel|grief|grieving|mourning|therapy|therapist|"
    r"counsell?or|psychiatr|mental health|suicid|self.harm|"
    r"divorce|breakup|broke up|marriage|marital|affair|dating|"
    r"my (wife|husband|partner|girlfriend|boyfriend|ex)\b|"
    r"argument with|fell out with|falling out|estranged|"
    r"feeling (down|low|awful|terrible|hopeless)|"
    r"medication|antidepressant"
    r")", re.I)


def is_excluded(text: str) -> bool:
    """True when this text is about something Jarvis must not analyse.

    A narrow false positive is fine — "I have a good relationship with this
    codebase" being skipped costs nothing — and the check is kept simple enough
    that its behaviour is obvious rather than clever.
    """
    return bool(EXCLUDED.search(text or ""))


def filter_out(texts: list[str]) -> list[str]:
    return [t for t in texts if not is_excluded(t)]
