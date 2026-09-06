"""Pending approvals and standing grants, persisted (§8, §40).

Replaces a module-level Map whose TTL is only checked on redemption. Three real
consequences of that design, all verified in the current code:

  * A token minted and never answered keeps its captured arguments for the life
    of the process — there is no sweep.
  * Every pending approval is lost on restart, so "delete this folder?" asked at
    5pm is simply gone at 5:05. TEST 12 expects state to survive per its
    persistence policy.
  * A background job may wait hours, far past any token TTL, so the existing
    design needs an entirely separate escalation path with its own durable
    record. One persisted approval covers both.

The same-turn rule is preserved and strengthened. An approval records the turn
that ASKED, and `resolve()` refuses a resolution carrying that same turn id: a
model cannot mint a question and answer it itself. In the current implementation
this protection is skipped whenever either side lacks a turn id — which is
exactly the Live voice path, where it never fires at all. Here turn ids are
required by CallContext, so there is no such gap to fall through.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable

from ..capabilities import CapabilitySpec
from ..db import get_db
from ..events import EventType, bus as default_bus
from ..events.bus import EventBus
from ..jscompat import now_iso
from .context import CallContext
from .decide import Grant


class Resolution(str, Enum):
    """§8: "Confirmation should support ALLOW / DENY / CANCEL / TIMEOUT.\""""

    PENDING = "pending"
    ALLOW = "allow"
    DENY = "deny"
    CANCEL = "cancel"
    TIMEOUT = "timeout"


class SameTurnRefused(RuntimeError):
    """An approval was resolved by the very turn that requested it."""


@dataclass(frozen=True)
class Approval:
    id: str
    operation_id: str
    capability: str
    args: dict[str, Any]
    risk: str
    session_id: str
    turn_id: str
    surface: str
    reason: str | None
    status: Resolution
    requested_at: str
    resolved_at: str | None = None
    resolved_by: str | None = None

    @property
    def is_pending(self) -> bool:
        return self.status is Resolution.PENDING


def _redact(args: dict[str, Any], spec: CapabilitySpec) -> dict[str, Any]:
    """§25: never log secrets. Which arguments are secret is declared by the
    capability, so no call site has to know."""
    if not spec.redact_args:
        return dict(args)
    return {k: ("<redacted>" if k in spec.redact_args else v) for k, v in args.items()}


def _row_to_approval(row: Any) -> Approval:
    return Approval(
        id=row["id"],
        operation_id=row["operation_id"],
        capability=row["capability"],
        args=json.loads(row["args"]) if row["args"] else {},
        risk=row["risk"],
        session_id=row["session_id"],
        turn_id=row["turn_id"],
        surface=row["surface"],
        reason=row["reason"],
        status=Resolution(row["status"]),
        requested_at=row["requested_at"],
        resolved_at=row["resolved_at"],
        resolved_by=row["resolved_by"],
    )


def request(
    spec: CapabilitySpec,
    args: dict[str, Any],
    ctx: CallContext,
    reason: str,
    event_bus: EventBus | None = None,
) -> Approval:
    """Record that a human decision is needed, and announce it.

    Idempotent on `operation_id` (§49): the same logical request delivered twice
    returns the existing approval rather than asking the user the same question
    again.
    """
    db = get_db()
    existing = db.execute(
        "SELECT * FROM approvals WHERE operation_id = ?", (ctx.operation_id,)
    ).fetchone()
    if existing is not None:
        return _row_to_approval(existing)

    approval = Approval(
        id=f"apr_{uuid.uuid4().hex[:12]}",
        operation_id=ctx.operation_id,
        capability=spec.name,
        args=_redact(args, spec),
        risk=spec.risk.value,
        session_id=ctx.session_id,
        turn_id=ctx.turn_id,
        surface=ctx.surface.value,
        reason=reason,
        status=Resolution.PENDING,
        requested_at=now_iso(),
    )
    db.execute(
        "INSERT INTO approvals (id, operation_id, capability, args, risk, session_id, "
        "turn_id, surface, reason, status, requested_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            approval.id, approval.operation_id, approval.capability,
            json.dumps(approval.args), approval.risk, approval.session_id,
            approval.turn_id, approval.surface, approval.reason,
            approval.status.value, approval.requested_at,
        ),
    )
    (event_bus or default_bus).publish(
        EventType.APPROVAL_REQUESTED,
        {
            "id": approval.id, "capability": approval.capability,
            "risk": approval.risk, "reason": approval.reason,
            "sessionId": approval.session_id, "surface": approval.surface,
        },
    )
    return approval


def resolve(
    approval_id: str,
    resolution: Resolution,
    resolving_turn_id: str,
    resolved_by: str = "user",
    event_bus: EventBus | None = None,
) -> Approval:
    """Answer a pending approval.

    Refuses if `resolving_turn_id` is the turn that asked. That is the model
    minting a question and answering it in the same breath — reproduced live on
    the voice path, where the protection currently cannot fire at all.
    """
    if resolution is Resolution.PENDING:
        raise ValueError("PENDING is not a resolution")

    db = get_db()
    row = db.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    if row is None:
        raise KeyError(f"No such approval: {approval_id}")
    approval = _row_to_approval(row)

    if not approval.is_pending:
        # Already answered. Return what was decided rather than overwriting it —
        # a second click must not flip a denial into an approval.
        return approval

    if resolving_turn_id == approval.turn_id:
        raise SameTurnRefused(
            f"{approval.capability} was asked about and answered within the same "
            "turn; a real reply from you has to come in between."
        )

    resolved_at = now_iso()
    db.execute(
        "UPDATE approvals SET status = ?, resolved_at = ?, resolved_by = ? WHERE id = ?",
        (resolution.value, resolved_at, resolved_by, approval_id),
    )
    updated = Approval(
        **{**approval.__dict__, "status": resolution,
           "resolved_at": resolved_at, "resolved_by": resolved_by}
    )
    (event_bus or default_bus).publish(
        EventType.APPROVAL_RESOLVED,
        {
            "id": updated.id, "capability": updated.capability,
            "resolution": resolution.value, "sessionId": updated.session_id,
        },
    )
    return updated


def get(approval_id: str) -> Approval | None:
    row = get_db().execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    return _row_to_approval(row) if row else None


def pending(session_id: str | None = None) -> list[Approval]:
    """Everything still awaiting an answer — survives a restart, which is the
    entire point of persisting these."""
    db = get_db()
    if session_id is None:
        rows = db.execute(
            "SELECT * FROM approvals WHERE status = 'pending' ORDER BY requested_at"
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM approvals WHERE status = 'pending' AND session_id = ? "
            "ORDER BY requested_at",
            (session_id,),
        ).fetchall()
    return [_row_to_approval(r) for r in rows]


def expire_older_than(cutoff_iso: str, event_bus: EventBus | None = None) -> int:
    """Mark stale pending approvals as TIMEOUT (§8's fourth outcome).

    A real sweep, rather than a TTL noticed only if someone happens to redeem the
    token. An unanswered question should visibly time out, not linger forever
    holding its arguments.
    """
    db = get_db()
    rows = db.execute(
        "SELECT * FROM approvals WHERE status = 'pending' AND requested_at < ?",
        (cutoff_iso,),
    ).fetchall()
    for row in rows:
        db.execute(
            "UPDATE approvals SET status = ?, resolved_at = ?, resolved_by = ? WHERE id = ?",
            (Resolution.TIMEOUT.value, now_iso(), "system", row["id"]),
        )
        (event_bus or default_bus).publish(
            EventType.APPROVAL_RESOLVED,
            {"id": row["id"], "capability": row["capability"],
             "resolution": Resolution.TIMEOUT.value, "sessionId": row["session_id"]},
        )
    return len(rows)


# --- standing grants (§8) ----------------------------------------------------


def grant(capability: str, session_id: str | None = None,
          expires_at: str | None = None, note: str | None = None) -> str:
    grant_id = f"grn_{uuid.uuid4().hex[:12]}"
    get_db().execute(
        "INSERT INTO permission_grants (id, capability, session_id, granted_at, "
        "expires_at, note) VALUES (?, ?, ?, ?, ?, ?)",
        (grant_id, capability, session_id, now_iso(), expires_at, note),
    )
    return grant_id


def revoke(grant_id: str) -> None:
    get_db().execute(
        "UPDATE permission_grants SET revoked_at = ? WHERE id = ?", (now_iso(), grant_id)
    )


def active_grants(session_id: str | None = None) -> list[Grant]:
    """Live grants, in the shape the pure policy function consumes.

    Converted here rather than letting the policy read the database: keeping
    `decide()` free of I/O is what makes its floors exhaustively testable.
    """
    from datetime import datetime

    def _epoch(iso: str | None) -> float | None:
        if not iso:
            return None
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()

    rows = get_db().execute(
        "SELECT * FROM permission_grants WHERE revoked_at IS NULL"
    ).fetchall()
    out: list[Grant] = []
    for row in rows:
        if session_id is not None and row["session_id"] not in (None, session_id):
            continue
        out.append(
            Grant(
                capability=row["capability"],
                granted_at=_epoch(row["granted_at"]) or 0.0,
                expires_at=_epoch(row["expires_at"]),
                session_id=row["session_id"],
            )
        )
    return out
