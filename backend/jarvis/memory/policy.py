"""What happens to a candidate memory — the one decision point (§22).

Pure: no database, no side effects, no preference read unless the caller omits
the trust level. That is what lets its whole behaviour be checked as an
exhaustive truth table, and it is why every caller asks this rather than
hardcoding "always ask" or "just save it" for itself.

**Two things always require approval, at every trust level, with no override:**
a candidate that conflicts with an existing memory (resolving a conflict changes
or duplicates something that already exists, and that is never done silently),
and a candidate with no usable confidence score (a caller that cannot say how
sure it is does not get the benefit of the doubt).

`ask` is `Infinity` rather than merely a high number, deliberately: that is what
makes the default level reproduce approval-first behaviour for EVERY score a
model could return, including a buggy one above 1.0.
"""

from __future__ import annotations

import math
from typing import Any

#: The confidence a candidate must meet or exceed to auto-save, per trust level.
THRESHOLDS: dict[str, float] = {"ask": math.inf, "balanced": 0.85, "auto": 0.0}

REQUIRE_APPROVAL = "require-approval"
AUTO_APPROVE = "auto-approve"


def decide(candidate: dict[str, Any] | None = None, trust: str | None = None) -> str:
    """'require-approval' or 'auto-approve'.

    `trust` overrides the saved preference — passed by tests so they never have
    to mutate prefs; real callers omit it and get the user's actual setting.
    """
    candidate = candidate or {}
    if trust not in THRESHOLDS:
        from ..prefs import get_prefs  # local: keeps this module pure for tests

        trust = str(get_prefs().get("memoryTrust") or "ask")
    threshold = THRESHOLDS.get(trust, THRESHOLDS["ask"])

    if candidate.get("conflictsWithId") or candidate.get("conflictWith"):
        return REQUIRE_APPROVAL

    confidence = candidate.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        return REQUIRE_APPROVAL
    if math.isnan(confidence) or math.isinf(confidence):
        return REQUIRE_APPROVAL

    return AUTO_APPROVE if confidence >= threshold else REQUIRE_APPROVAL
