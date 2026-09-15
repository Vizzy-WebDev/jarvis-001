"""Does this proposal apply itself, or wait for the user?

One decision point, asked by every caller — the background cycle, the screen's
approve button, and a conversational suggestion — so none of them hardcodes
"always ask" or "just do it" for itself. Pure, so its whole behaviour is a truth
table.

**Three floors no trust level can lower**, each from a settled requirement:

1. The kind must be a rule or a setting. A Skill or a code change always asks —
   Jarvis does not edit itself.
2. The source tier must be 1: only Jarvis's OWN directly-observed history may
   auto-apply. Anything read from documentation, a community or the web always
   asks, however solid it looks.
3. A proposal conflicting with an existing rule always asks, since resolving a
   conflict changes something that already exists.

Beneath those, evidence count is the damping floor: fewer than the trust level's
minimum distinct outcomes and it waits. That is what stops a single job failure
from minting a permanent rule, and what stops learn → apply → learn from
oscillating.
"""

from __future__ import annotations

import math
from typing import Any

#: Unlike memory, there is no confidence dial here — the floors do the real
#: gating, so trust only widens or narrows what counts as enough evidence.
#: `ask` is Infinity so the default reproduces "nothing applies itself".
MIN_EVIDENCE_BY_TRUST: dict[str, float] = {"ask": math.inf, "balanced": 2, "auto": 1}

AUTO_APPLY = "auto-apply"
REQUIRE_APPROVAL = "require-approval"

APPLIABLE_KINDS = ("rule", "setting")


def decide(proposal: dict[str, Any] | None = None, trust: str | None = None) -> str:
    proposal = proposal or {}

    if proposal.get("kind") not in APPLIABLE_KINDS:
        return REQUIRE_APPROVAL
    if proposal.get("conflictWith"):
        return REQUIRE_APPROVAL
    try:
        if int(proposal.get("sourceTier")) != 1:
            return REQUIRE_APPROVAL
    except (TypeError, ValueError):
        return REQUIRE_APPROVAL

    if trust not in MIN_EVIDENCE_BY_TRUST:
        from ..prefs import get_prefs

        trust = str(get_prefs().get("improvementTrust") or "ask")
    minimum = MIN_EVIDENCE_BY_TRUST.get(trust, MIN_EVIDENCE_BY_TRUST["ask"])

    evidence = proposal.get("evidence")
    count = len(evidence) if isinstance(evidence, (list, tuple)) else 0
    return AUTO_APPLY if count >= minimum else REQUIRE_APPROVAL
