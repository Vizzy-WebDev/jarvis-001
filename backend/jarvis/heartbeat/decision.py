"""How urgent is this, really — and during quiet hours, does it clear the bar?

One model call answers both, which halves the cost of a judgment that has to be
made for every finding on a roster that is routinely rate limited.

**Reasoned against what is actually known about the user, never matched against
a fixed list of emergency categories.** A category list is wrong the moment
someone's life changes, and it is wrong in the direction that matters: the thing
that suddenly matters most to them this month is exactly the thing not on it.

**No model, or a reply that cannot be read, is Tier 3 — never Tier 1, never an
emergency.** Silence is the safe direction for a failure here: the finding is
still recorded, and the worst case is that the user reads it later instead of
being interrupted. The opposite default would wake someone at 3am because a
model was rate limited.

Judging the STAKES rather than the wording is stated explicitly in the
instructions. A job's outbox summary is politely worded whatever is actually at
risk, and reading tone as urgency would rank it by phrasing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

VALID_TIERS = (1, 2, 3)


@dataclass(frozen=True)
class Verdict:
    tier: int
    reason: str
    emergency: bool = False
    emergency_reason: str | None = None


def _held(reason: str) -> Verdict:
    return Verdict(tier=3, reason=reason)


def _system(quiet: bool) -> str:
    return " ".join([
        "You decide whether something Jarvis just noticed is worth interrupting the user "
        "about, and how urgently.",
        "Weigh it against what is actually known about their priorities below. Never match it "
        "against a fixed list of emergency categories or keywords — that stops being right the "
        "moment their life changes. Real money, real risk, something irreversible, or something "
        "they have clearly said matters right now are things to reason from, not a checklist.",
        "Tier 1 needs them now. Tier 2 is worth mentioning at a natural moment. Tier 3 is worth "
        "a record and nothing more.",
        ("It is currently their quiet hours. Nothing should reach them now UNLESS this is a "
         "genuine emergency: urgent AND costly or harmful if it waits. Set that bar noticeably "
         "higher than an ordinary daytime \"important\". If you are genuinely in doubt, it is "
         "NOT an emergency.") if quiet else "It is not currently quiet hours.",
        "Judge the real stakes being described, never the tone it happens to be phrased in. A "
        "background job asks politely however urgent the situation actually is.",
        'Reply with JSON only: {"tier": 1, "reason": "one short sentence", "emergency": false, '
        '"emergencyReason": null}.',
    ])


def decide_attention(finding: dict[str, Any], *, now: datetime | None = None) -> Verdict:
    from ..gateway.client import ask
    from ..gateway.routing import Task
    from ..memory.store import approved_memories_text
    from .quiet_hours import is_quiet_now

    quiet = is_quiet_now(now)
    try:
        known = approved_memories_text()
    except Exception:  # noqa: BLE001 — a memory read failing must not decide urgency
        logger.exception("could not read memories for an urgency decision")
        known = ""

    prompt = "\n\n".join([
        (f"What is known about their priorities:\n{known}" if known else
         "Nothing is known about their priorities beyond this finding itself."),
        f"What Jarvis just noticed: {finding.get('summary')}",
    ])

    try:
        answer = ask(prompt, system=_system(quiet), want_json=True,
                     task=Task(text=str(finding.get("summary") or ""), background=True,
                               needs_tools=False))
    except Exception as err:  # noqa: BLE001 — no model available is the common case
        logger.info("no model could judge a finding: %s", err)
        return _held("No model was available to judge this, so it was only recorded.")

    data = answer.data
    if not isinstance(data, dict):
        return _held("The urgency check could not be read, so this was only recorded.")

    tier = data.get("tier") if data.get("tier") in VALID_TIERS else 3
    reason = data.get("reason")
    emergency_reason = data.get("emergencyReason")
    # `emergency` means nothing outside quiet hours, and is forced false there
    # regardless of what the model said — it has no other effect to have.
    emergency = bool(quiet and data.get("emergency") is True and emergency_reason)
    return Verdict(
        tier=tier,
        reason=reason if isinstance(reason, str) and reason else "No reason given.",
        emergency=emergency,
        emergency_reason=str(emergency_reason) if emergency else None,
    )
