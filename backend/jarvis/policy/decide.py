"""The permission decision — pure, deterministic, model-independent (§7).

Follows the pattern the audit found to be the strongest thing in the existing
codebase: a side-effect-free decision function that every caller asks and none
second-guesses. The three existing policy modules were verified by exhaustive
truth table and their safety floors genuinely hold; this is built the same way so
it can be checked the same way.

The floors below are not overridable by any autonomy level, any surface, or any
instruction from the model or the user in the moment:

1. **HIGH risk always requires a human decision** unless a standing grant names
   that exact capability. A wildcard grant never covers HIGH — "allow file tools"
   must not silently authorise deletion.
2. **Pre-consent does not cover HIGH.** Agreeing to a scheduled task is not
   agreeing to whatever it later decides to delete. This is stricter than the
   current implementation, which passes a blanket auto-confirm for scheduled work
   and briefings.
3. **Voice is not a bypass.** Surface is recorded and never relaxes anything —
   the defect §8 explicitly forbids.
4. **Low speech confidence can only ever ADD a confirmation**, never remove one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum

from ..capabilities import CapabilitySpec, Risk
from .context import Autonomy, CallContext


class Outcome(str, Enum):
    #: Run it now.
    ALLOWED = "allowed"
    #: A human must answer first. Interactive callers ask; escalating callers park.
    NEEDS_APPROVAL = "needs_approval"
    #: Cannot proceed and asking will not help (e.g. not in the allowed set).
    REFUSED = "refused"


@dataclass(frozen=True)
class Grant:
    """A standing permission the user has already given (§8).

    `capability` is an exact name, or "*" for a wildcard. A wildcard deliberately
    cannot cover HIGH risk.
    """

    capability: str
    granted_at: float
    expires_at: float | None = None
    #: None means "any session"; a session id scopes it to one conversation.
    session_id: str | None = None

    def covers(self, spec: CapabilitySpec, ctx: CallContext, now: float) -> bool:
        if self.expires_at is not None and now >= self.expires_at:
            return False
        if self.session_id is not None and self.session_id != ctx.session_id:
            return False
        if self.capability == "*":
            # Floor 1: a blanket grant never authorises a high-risk action.
            return spec.risk is not Risk.HIGH
        return self.capability == spec.name


@dataclass(frozen=True)
class PolicyResult:
    outcome: Outcome
    #: Plain-language explanation, safe to show the user verbatim.
    reason: str
    #: True when the caller must park the request rather than ask inline.
    escalate: bool = False


def decide(
    spec: CapabilitySpec,
    ctx: CallContext,
    grants: list[Grant] | None = None,
    allowed_names: frozenset[str] | None = None,
    now: float | None = None,
) -> PolicyResult:
    """Decide whether this call may proceed.

    `allowed_names`, when given, is an allowlist enforced HERE rather than by the
    caller. In the Node implementation the equivalent check lives in the turn
    runner, so every other caller — the scheduler, briefings, the Live voice path
    — gets no enforcement at all. A restriction that only one caller applies is
    not a restriction.
    """
    now = time.time() if now is None else now
    grants = grants or []

    if allowed_names is not None and spec.name not in allowed_names:
        return PolicyResult(
            Outcome.REFUSED,
            f"{spec.name} is not available for this task.",
        )

    standing = next((g for g in grants if g.covers(spec, ctx, now)), None)

    # Floor 4: low confidence can only add a confirmation, never remove one.
    if spec.risk is Risk.LOW and not ctx.low_confidence:
        return PolicyResult(Outcome.ALLOWED, "Low-risk action.")

    if spec.risk is Risk.LOW and ctx.low_confidence:
        if ctx.autonomy is Autonomy.INTERACTIVE:
            return PolicyResult(
                Outcome.NEEDS_APPROVAL,
                "I didn't catch that clearly — confirming before I act.",
            )
        # Nobody is present to re-hear it; a low-risk action still proceeds.
        return PolicyResult(Outcome.ALLOWED, "Low-risk action.")

    if standing is not None:
        return PolicyResult(
            Outcome.ALLOWED,
            f"You've already authorised {spec.name}.",
        )

    if spec.risk is Risk.MEDIUM and ctx.autonomy is Autonomy.PRE_CONSENTED:
        return PolicyResult(
            Outcome.ALLOWED,
            "Part of a task you set up in advance.",
        )

    # Floors 1-3: everything else needs a human, and where that human is
    # determines whether we ask now or park the question.
    if ctx.autonomy is Autonomy.ESCALATE:
        return PolicyResult(
            Outcome.NEEDS_APPROVAL,
            f"{spec.name} needs your go-ahead before it can run.",
            escalate=True,
        )
    if ctx.autonomy is Autonomy.PRE_CONSENTED:
        # Floor 2: setting up a task is not blanket consent for a high-risk
        # action it later decides to take. Park it rather than run it.
        return PolicyResult(
            Outcome.NEEDS_APPROVAL,
            f"{spec.name} is high-risk, so it needs your go-ahead even inside a "
            "task you scheduled.",
            escalate=True,
        )
    return PolicyResult(
        Outcome.NEEDS_APPROVAL,
        f"{spec.name} needs confirming before I run it.",
    )
