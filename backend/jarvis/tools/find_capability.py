"""Find one of the less common abilities by what the user actually wants.

Declaring every capability on every turn is not free: measured on the Node app,
the full set was ~150,000 characters sent on "hello" as much as on anything else,
and it was the single largest cause of flat, instruction-ignoring replies. So a
large registry declares its core set plus whatever this turn has unlocked, and
this tool is how the rest becomes reachable.

**How unlocking works here, and why it is not a `ctx` injection.** This tool
returns the names it matched under an `unlock` key; the orchestrator adds them to
the tools declared for the remaining steps of that turn. The mechanism is
therefore plain data in a tool result — testable on its own, with no tool
reaching back into the loop that called it, and no import edge back to the
registry loader.
"""

from __future__ import annotations

import re
from typing import Any

from ..capabilities import CapabilityRegistry, CapabilitySpec, Risk

MAX_MATCHES = 6
_WORD = re.compile(r"[a-z0-9]+")

#: Filler that appears in nearly every capability description, so matching on it
#: returns the whole registry — which is the same as returning nothing useful.
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "your", "you", "this", "that", "what",
    "when", "how", "can", "use", "user", "asks", "about", "them", "something",
    "anything", "please", "want", "wants", "need", "needs", "help", "me", "my",
})


def _terms(text: str) -> list[str]:
    return [w for w in _WORD.findall((text or "").lower())
            if len(w) > 2 and w not in _STOPWORDS]


def _score(spec: CapabilitySpec, terms: list[str]) -> int:
    name = spec.name.lower().replace("_", " ")
    description = (spec.description or "").lower()
    score = 0
    for term in terms:
        if term in name:
            score += 3
        if term in description:
            score += 1
    return score


def build(registry: CapabilityRegistry) -> list[CapabilitySpec]:
    def _run(intent: str = "") -> dict[str, Any]:
        terms = _terms(intent)
        if not terms:
            # Same shape as "nothing matched", deliberately: one result shape
            # means no caller has to special-case an empty search.
            return {"ok": False, "found": [], "unlock": [],
                    "error": "Say in a few words what's needed — a verb helps."}
        scored = [(spec, _score(spec, terms)) for spec in registry.list()]
        best = max((n for _, n in scored), default=0)
        # Two gates, both needed: a hard floor so one incidental word in a long
        # description is not a match, and a relative floor so a genuinely strong
        # match is not diluted by six weak ones.
        floor = max(2, best / 2)
        matches = sorted(((s, n) for s, n in scored if n >= floor),
                         key=lambda pair: (-pair[1], pair[0].name))[:MAX_MATCHES]
        if not matches:
            # An honest "no" beats offering something unrelated (§45).
            return {"ok": True, "found": [], "unlock": [],
                    "note": f"I have nothing that does that ({intent})."}
        return {
            "ok": True,
            "found": [{"name": s.name, "description": s.description} for s, _ in matches],
            "unlock": [s.name for s, _ in matches],
            "note": "These are callable now, in this same turn.",
        }

    return [CapabilitySpec(
        id="builtin.find_capability",
        name="find_capability",
        description=("Look up one of your less common abilities by what the user wants — "
                     "scheduling something, controlling the computer, reading a spreadsheet, "
                     "or anything connected through another app. Describe what's needed in a "
                     "few plain words; matching abilities become callable right away, in this "
                     "same turn."),
        input_schema={"type": "object", "properties": {
            "intent": {"type": "string",
                       "description": 'A few words, e.g. "schedule a reminder".'}},
            "required": ["intent"]},
        risk=Risk.LOW,
        handler=_run,
        timeout_s=5.0,
        tags=frozenset({"core"}),
    )]
