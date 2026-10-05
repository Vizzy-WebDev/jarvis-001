"""The call context every capability invocation must carry.

This type exists because of a defect verified live in the current system. The
Gemini Live voice path calls `invoke(name, args)` with **no context argument at
all**. The same-turn confirm-token refusal keys off `ctx.turnId` and the
voice-clarity tier keys off `ctx.lowConfidence`, so both protections are silently
inoperative in voice mode — reproduced by minting a token and redeeming it
immediately in the next call, which ran.

§8 names that exact scenario: "A dangerous action should never bypass the gate
simply because it originated from voice mode."

So context is a REQUIRED, TYPED argument with no default. A caller that forgets
it fails at the call site instead of silently losing its protections. `surface`
records where a call came from for observability and policy — never as grounds
to skip the gate.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum


class Surface(str, Enum):
    """Where the call originated. Recorded, never a reason to relax the gate."""

    TEXT = "text"
    VOICE = "voice"
    JOB = "job"
    SCHEDULED = "scheduled"
    CONTROL = "control"       # computer control loop


class Autonomy(str, Enum):
    """How an approval can actually be obtained for this call.

    Not a permission level — a statement about who is present to answer.
    """

    #: A human is here now and can be asked, with a read-back.
    INTERACTIVE = "interactive"
    #: The user consented to this work in advance (a scheduled task they created).
    PRE_CONSENTED = "pre_consented"
    #: Nobody is present. A question must be parked for later, never minted as a
    #: short-lived token nobody will answer — a background job may wait hours.
    ESCALATE = "escalate"


@dataclass(frozen=True)
class CallContext:
    session_id: str
    turn_id: str
    surface: Surface
    autonomy: Autonomy
    #: Speech recognition confidence was low enough to warrant a read-back even
    #: for something that would not otherwise need one.
    low_confidence: bool = False
    #: §49 — a stable id for this logical operation. Two deliveries of the same
    #: request carry the same id, so "create a reminder for 9am" executed twice
    #: creates one reminder.
    operation_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        if not self.session_id or not self.turn_id:
            raise ValueError(
                "session_id and turn_id are required — without a turn id the "
                "same-turn approval bypass cannot be detected"
            )
