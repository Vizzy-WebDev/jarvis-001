"""Self-improvement over HTTP: what Jarvis has learned about its own work.

A surface over `improvement/`, which holds every rule about what may change and
what may not. Two of those rules decide the shape of this file, and neither is
re-checked here — a route enforcing them would be a second answer to a question
the policy already answers, and a second answer is what eventually disagrees.

**Only Jarvis's own directly-observed history may ever apply itself.** Anything
read from outside always asks, however solid it looks. So approving goes through
`apply`, which refuses anything that is not a rule or a setting whatever the
caller believed it was approving.

**Jarvis never edits its own code.** A proposal needing real work produces a
BRIEF for a person's coding assistant, and generating that brief IS the approval
action for those kinds — which is why `implementation-prompt` is a POST that
changes something rather than a read.

**A refused undo is a 200, not an error.** The caller asked a real question and
got a real answer: the value changed since, here is what it is now, say so and
ask again with `force`. Nothing went wrong, and returning 4xx would make the
screen treat a normal branch as a failure.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from ..improvement import apply as apply_module
from ..improvement import implementation_prompt, store
from ..prefs import get_prefs

router = APIRouter(prefix="/api")

_UNDERSCORE = re.compile(r"_([a-z])")


def _camel(row: dict[str, Any]) -> dict[str, Any]:
    """Database columns as the rest of this API spells them.

    It lives at the edge rather than in the store on purpose: the store's shapes
    are what the subsystem's own tests read, and rewriting them to suit one
    consumer is how a store ends up serving a screen instead of a subsystem.
    Some rows already carry camelCase aliases; a key that is already correct is
    simply written over itself.
    """
    return {_UNDERSCORE.sub(lambda m: m.group(1).upper(), key): value
            for key, value in row.items()}


def _rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_camel(row) for row in rows]


# --- where it stands -----------------------------------------------------------

@router.get("/improvement/status")
def status() -> dict[str, Any]:
    """The dial, the two budgets, and how much is waiting.

    The budgets are the honest part. Each module has a cadence floor of its own,
    and without a spend cap on top a fifteen-minute background tick checking a
    simple "enough unreviewed outcomes yet" count would fire on nearly every tick
    of a busy day. Showing what is left is what makes that legible rather than
    mysterious.
    """
    prefs = get_prefs()
    return {
        "enabled": prefs["improvementEnabled"],
        "trust": prefs["improvementTrust"],
        "research": prefs["improvementResearch"],
        "dailyBudgetRemaining": store.daily_budget_remaining(),
        "weeklyBudgetRemaining": store.weekly_budget_remaining(),
        "unreviewedOutcomes": store.count_unreviewed_outcomes(),
        "pendingProposals": len(store.list_proposals("pending")),
    }


# --- suggestions ---------------------------------------------------------------

@router.get("/improvement/proposals")
def proposals(status: str | None = "pending") -> dict[str, Any]:
    return {"proposals": _rows(store.list_proposals(status or None))}


@router.post("/improvement/proposals/{proposal_id}/approve")
def approve(proposal_id: str):
    """Apply it, if it is a kind that may be applied at all.

    The refusal for the other kinds comes from `apply` rather than from a check
    here, so there is exactly one place that decides it.
    """
    try:
        applied = apply_module.apply_proposal(proposal_id)
    except KeyError:
        return JSONResponse({"ok": False, "error": "That suggestion no longer exists."},
                            status_code=404)
    except ValueError as err:
        return JSONResponse({"ok": False, "error": str(err)}, status_code=400)
    return {"ok": True, "applied": applied,
            "proposal": _camel(store.get_proposal(proposal_id) or {})}


@router.post("/improvement/proposals/{proposal_id}/reject")
def reject(proposal_id: str):
    if store.get_proposal(proposal_id) is None:
        return JSONResponse({"ok": False, "error": "That suggestion no longer exists."},
                            status_code=404)
    store.set_proposal_status(proposal_id, "rejected")
    return {"ok": True, "proposal": _camel(store.get_proposal(proposal_id) or {})}


@router.delete("/improvement/proposals/{proposal_id}")
def delete_proposal(proposal_id: str) -> dict[str, Any]:
    """Permanent, from the rejected bin only — the same archive-then-delete
    discipline the rules and lessons below keep."""
    store.delete_proposal(proposal_id)
    return {"ok": True}


@router.post("/improvement/proposals/{proposal_id}/restore")
def restore_proposal(proposal_id: str):
    """Out of the bin and back into the queue — not applied, just pending again."""
    if store.get_proposal(proposal_id) is None:
        return JSONResponse({"ok": False, "error": "That suggestion no longer exists."},
                            status_code=404)
    store.set_proposal_status(proposal_id, "pending")
    return {"ok": True, "proposal": _camel(store.get_proposal(proposal_id) or {})}


@router.post("/improvement/proposals/{proposal_id}/implementation-prompt")
def brief(proposal_id: str, body: dict[str, Any] = Body(default_factory=dict)):
    """A ready-to-paste brief for whichever coding assistant is named.

    The target is free text, deliberately — a fixed list of assistant names would
    be wrong the week after it was written. The brief is built from a static,
    hand-written architecture summary and never reads the actual source tree.
    """
    try:
        built = implementation_prompt.build(
            proposal_id, str(body.get("target") or "your coding assistant"))
    except KeyError:
        return JSONResponse({"ok": False, "error": "That suggestion no longer exists."},
                            status_code=404)
    except ValueError as err:
        return JSONResponse({"ok": False, "error": str(err)}, status_code=400)
    return {"ok": True, **built, "proposal": _camel(store.get_proposal(proposal_id) or {})}


# --- rules: the things that actually apply themselves --------------------------

@router.get("/improvement/rules")
def rules(includeArchived: bool = False) -> dict[str, Any]:
    return {"rules": _rows(store.list_rules(include_archived=includeArchived))}


@router.post("/improvement/rules/{rule_id}/toggle")
def toggle(rule_id: str, body: dict[str, Any] = Body(default_factory=dict)):
    """Mute or unmute, with the change recorded so it can be undone like any
    other. Muting by hand is also exactly what makes a later undo refuse rather
    than clobber — see `apply.undo_change`."""
    before = store.get_rule(rule_id)
    if before is None:
        return JSONResponse({"ok": False, "error": "That rule no longer exists."},
                            status_code=404)
    active = bool(body.get("active", not before["active"]))
    store.set_rule_active(rule_id, active)
    after = store.get_rule(rule_id) or {}
    # These two keys, and no others: `apply.undo_change` compares the LIVE rule
    # against this exact pair before restoring, so an extra field here would make
    # every undo of this change refuse as though somebody had edited it.
    store.record_change(kind="rule", target=rule_id,
                        before={"text": before["text"], "active": bool(before["active"])},
                        after={"text": after.get("text"), "active": active},
                        reason="Muted on the Self-Improvement screen." if not active
                               else "Unmuted on the Self-Improvement screen.")
    return {"ok": True, "rule": _camel(after)}


@router.patch("/improvement/rules/{rule_id}")
def edit_rule(rule_id: str, body: dict[str, Any] = Body(default_factory=dict)):
    """A rule is the one thing here whose text is directly editable, and the edit
    rides the same undo machinery a freshly-applied rule already uses."""
    before = store.get_rule(rule_id)
    if before is None:
        return JSONResponse({"ok": False, "error": "That rule no longer exists."},
                            status_code=404)
    text = str(body.get("text") or "").strip()
    if not text:
        return JSONResponse({"ok": False, "error": "A rule needs some text."}, status_code=400)
    store.update_rule_text(rule_id, text)
    after = store.get_rule(rule_id) or {}
    store.record_change(kind="rule", target=rule_id,  # the same two keys — see above
                        before={"text": before["text"], "active": bool(before["active"])},
                        after={"text": after.get("text"), "active": bool(after.get("active"))},
                        reason="Edited on the Self-Improvement screen.")
    return {"ok": True, "rule": _camel(after)}


@router.post("/improvement/rules/{rule_id}/archive")
def archive_rule(rule_id: str):
    if store.get_rule(rule_id) is None:
        return JSONResponse({"ok": False, "error": "That rule no longer exists."},
                            status_code=404)
    store.archive_rule(rule_id)
    return {"ok": True, "rule": _camel(store.get_rule(rule_id) or {})}


@router.post("/improvement/rules/{rule_id}/restore")
def restore_rule(rule_id: str):
    if store.get_rule(rule_id) is None:
        return JSONResponse({"ok": False, "error": "That rule no longer exists."},
                            status_code=404)
    store.restore_rule(rule_id)
    return {"ok": True, "rule": _camel(store.get_rule(rule_id) or {})}


@router.delete("/improvement/rules/{rule_id}")
def delete_rule(rule_id: str) -> dict[str, Any]:
    """Permanent. Reachable only from an archived row, never from the live list —
    a rule's own Undo in the change log depends on the row still existing, so a
    hard delete must always go through archive first."""
    store.delete_rule(rule_id)
    return {"ok": True}


# --- lessons: observations, not behaviour ---------------------------------------

@router.get("/improvement/lessons")
def lessons(status: str | None = "active") -> dict[str, Any]:
    return {"lessons": _rows(store.list_lessons(status or None))}


@router.post("/improvement/lessons/{lesson_id}/archive")
def archive_lesson(lesson_id: str):
    store.set_lesson_status(lesson_id, "archived")
    return {"ok": True, "lesson": _camel(store.get_lesson(lesson_id) or {})}


@router.post("/improvement/lessons/{lesson_id}/restore")
def restore_lesson(lesson_id: str):
    store.set_lesson_status(lesson_id, "active")
    return {"ok": True, "lesson": _camel(store.get_lesson(lesson_id) or {})}


@router.delete("/improvement/lessons/{lesson_id}")
def delete_lesson(lesson_id: str) -> dict[str, Any]:
    store.delete_lesson(lesson_id)
    return {"ok": True}


# --- the change log, and undoing --------------------------------------------------

@router.get("/improvement/changes")
def changes() -> dict[str, Any]:
    return {"changes": _rows(store.list_changes())}


@router.post("/improvement/changes/{change_id}/undo")
def undo(change_id: str, body: dict[str, Any] = Body(default_factory=dict)):
    """`force` only after the screen has already shown "you changed this since —
    restore anyway?" and been told yes."""
    try:
        undone = apply_module.undo_change(change_id, force=bool(body.get("force")))
    except apply_module.UndoRefused as refused:
        # A real answer to a real question, not a failure.
        return {"ok": False, "reason": "changed_since", "message": str(refused),
                "live": refused.live, "expected": refused.expected}
    except KeyError:
        return JSONResponse({"ok": False, "error": "That change no longer exists."},
                            status_code=404)
    except ValueError as err:
        return JSONResponse({"ok": False, "error": str(err)}, status_code=400)
    # The undo is itself a change row — returned so the log can be re-read from
    # what came back rather than re-fetched.
    return {"ok": True, "change": _camel(undone)}


# --- raw outcomes ----------------------------------------------------------------

@router.get("/improvement/outcomes")
def outcomes() -> dict[str, Any]:
    """What has happened but not yet been reflected on. Nothing here is a
    conclusion — a single one structurally cannot become a rule."""
    return {"outcomes": _rows(store.list_unreviewed_outcomes(50))}


@router.post("/improvement/outcomes/lookup")
def outcomes_lookup(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    """The evidence behind a lesson, by id.

    A lesson names the outcomes it rests on, and being able to read them is what
    separates "it decided this" from "it decided this, and here is why".
    """
    ids = [str(i) for i in (body.get("ids") or [])]
    found = [store.get_outcome(i) for i in ids]
    return {"outcomes": _rows([row for row in found if row])}
