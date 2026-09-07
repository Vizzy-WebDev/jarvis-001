"""Approvals over HTTP — the human half of the confirmation gate (§8, §40).

Persisted, so a pending question survives a restart: the durable record of
consent is a row and a status, never a short-lived token in memory that a
background job waiting hours could never redeem.

**Answering happens in its own HTTP request, which is by construction a
different turn from the one that asked.** That is what makes the same-turn
refusal enforceable rather than aspirational — the model cannot mint a question
and answer it in the same breath, because it does not get to make this call at
all.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from ..assembly import get_registry
from ..capabilities.execute import execute_approved
from ..policy import Autonomy, CallContext, Surface
from ..policy import approvals as store
from ..policy.approvals import Resolution, SameTurnRefused
from ..session import get_active_session_id

router = APIRouter(prefix="/api")

_DECISIONS = {"allow": Resolution.ALLOW, "deny": Resolution.DENY, "cancel": Resolution.CANCEL}


def _public(approval: Any) -> dict[str, Any]:
    return {
        "id": approval.id,
        "capability": approval.capability,
        # Never the raw args: a redacted argument must not travel just because
        # the transport is local.
        "args": approval.safe_args,
        "risk": approval.risk,
        "reason": approval.reason,
        "sessionId": approval.session_id,
        "surface": approval.surface,
        "status": approval.status.value,
        "requestedAt": approval.requested_at,
    }


@router.get("/approvals")
def pending(session: str | None = None) -> dict[str, Any]:
    return {"approvals": [_public(a) for a in store.pending(session)]}


@router.post("/approvals/{approval_id}")
def decide(approval_id: str, body: dict[str, Any] = Body(default_factory=dict)):
    decision = _DECISIONS.get(str(body.get("decision") or "").lower())
    if decision is None:
        return JSONResponse({"error": "Say allow, deny, or cancel."}, status_code=400)

    approval = store.get(approval_id)
    if approval is None:
        return JSONResponse({"error": "That approval no longer exists."}, status_code=404)

    # A fresh turn id per request: this IS a later turn, and saying so honestly
    # is what lets the same-turn refusal stay strict everywhere else.
    resolving_turn = uuid.uuid4().hex

    if decision is not Resolution.ALLOW:
        resolved = store.resolve(approval_id, decision, resolving_turn)
        return {"approval": _public(resolved), "ran": False}

    # Some approvals are answered rather than executed: a control session is
    # already running and paused mid-action, waiting for this exact row. There
    # is nothing in the registry to call — the session performs the action
    # itself, having asked — so resolving IS the whole answer here.
    from ..control import session as control_session

    if control_session.is_waiting_for(approval_id):
        resolved = store.resolve(approval_id, decision, resolving_turn)
        return {"approval": _public(resolved), "ran": False, "delivered": True}

    ctx = CallContext(
        session_id=approval.session_id or get_active_session_id(),
        turn_id=resolving_turn,
        surface=Surface(approval.surface) if approval.surface else Surface.TEXT,
        autonomy=Autonomy.INTERACTIVE,
        operation_id=approval.operation_id,
    )
    try:
        result = execute_approved(approval_id, resolving_turn, ctx, registry=get_registry())
    except SameTurnRefused as err:
        return JSONResponse({"error": str(err)}, status_code=409)

    return {
        "approval": _public(store.get(approval_id)),
        "ran": True,
        "result": {"ok": result.ok, "outcome": result.outcome.value,
                   "error": result.error, "value": result.value},
    }
