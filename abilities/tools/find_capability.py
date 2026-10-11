"""Find one of the less common abilities by what the user actually wants.

Declaring every capability on every turn is not free: measured, the full
set was ~150,000 characters sent on "hello" as much as on anything else,
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

from typing import Any

from ..capabilities import CapabilityRegistry, CapabilitySpec, Risk
# The ranking itself is shared with the turn loop, which looks up what a request plainly
# points at before the model is asked anything (`capabilities/search.py`).
from ..capabilities.search import MAX_MATCHES, matches as _matches, terms as _terms

BRIEF_CHARS = 240


def _brief(description: str) -> str:
    """Enough to choose by, not the whole manual.

    A connected app's own tool description can run to thousands of characters
    (Notion's longest is ~8,700): returned in full, one search for "create a
    Notion page" was ~19,000 characters, and every tool it unlocks is declared
    right after WITH that full description anyway — the model paid for it twice,
    on the free tiers this app usually runs on.
    """
    lines = [line.strip() for line in (description or "").splitlines()]
    text = " ".join(line for line in lines if line and not line.startswith("#"))
    return text if len(text) <= BRIEF_CHARS else text[:BRIEF_CHARS].rsplit(" ", 1)[0] + "…"


def build(registry: CapabilityRegistry) -> list[CapabilitySpec]:
    def _run(intent: str = "") -> dict[str, Any]:
        terms = _terms(intent)
        if not terms:
            # Same shape as "nothing matched", deliberately: one result shape
            # means no caller has to special-case an empty search.
            return {"ok": False, "found": [], "unlock": [],
                    "error": "Say in a few words what's needed — a verb helps."}
        # A background worker's own tools are not answers to "can you do X":
        # nothing the user says can reach one, so offering it would be a lie.
        searchable = [spec for spec in registry.list() if not spec.has_tag("job")]
        matches = _matches(searchable, intent, limit=MAX_MATCHES)
        if not matches:
            # An honest "no" beats offering something unrelated (§45).
            return {"ok": True, "found": [], "unlock": [],
                    "note": f"I have nothing that does that ({intent})."}
        return {
            "ok": True,
            "found": [{"name": s.name, "description": _brief(s.description)} for s, _ in matches],
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
