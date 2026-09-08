"""Being taught something, proposing a change, and undoing one.

`record_lesson` is for something the user PLAINLY taught — "next time, just…" —
never for a one-off request. The trigger is their teaching, exactly as an
explicit request is the trigger for saving a memory.

Nothing here applies a change on its own: `record_lesson` files a lesson (an
observation), `suggest_improvement` files a proposal, and the policy decides
whether a proposal applies itself. That separation is the point.
"""

from __future__ import annotations

from typing import Any

from ..capabilities import CapabilitySpec, Risk
from ..improvement import apply, capture, store
from ..improvement.policy import AUTO_APPLY, decide


def _record_lesson(text: str = "", scope: str = "general") -> dict[str, Any]:
    body = (text or "").strip()
    if not body:
        return {"ok": False, "error": "There's nothing to record."}
    outcome = capture.record_explicit_teaching(body)
    lesson = store.create_lesson(text=body, scope=scope,
                                 evidence=[outcome["id"]] if outcome else [])
    return {"ok": True, "id": lesson["id"], "noted": lesson["text"],
            "note": ("Filed as something learned. It only changes how I work if it "
                     "turns out to recur.")}


def _suggest(title: str = "", rationale: str = "", kind: str = "rule",
             text: str = "") -> dict[str, Any]:
    if not (title or "").strip():
        return {"ok": False, "error": "A suggestion needs a title."}
    proposal = store.create_proposal(kind=kind, title=title.strip(), rationale=rationale,
                                     payload={"text": text or title},
                                     # Proposed in conversation, from one moment:
                                     # no accumulated evidence behind it yet.
                                     evidence=[], source_tier=1)
    verdict = decide(proposal)
    applied = None
    if verdict == AUTO_APPLY:
        applied = apply.apply_proposal(proposal["id"], approved_by="the policy")
    return {"ok": True, "id": proposal["id"], "title": proposal["title"],
            "applied": bool(applied),
            "note": ("Saved for the user to approve — a suggestion made in the moment "
                     "has no track record behind it yet." if applied is None
                     else "Applied.")}


def _review() -> dict[str, Any]:
    pending = store.list_proposals(status="pending")
    rules = store.list_rules(active_only=True)
    changes = store.list_changes(limit=5)
    return {"ok": True,
            "pending": [{"id": p["id"], "title": p["title"], "kind": p["kind"]}
                        for p in pending],
            "activeRules": [{"id": r["id"], "text": r["text"]} for r in rules],
            "recentChanges": [{"id": c["id"], "kind": c["kind"], "target": c["target"],
                               "undone": bool(c.get("undone_at"))} for c in changes]}


def _undo(change_id: str = "", force: bool = False) -> dict[str, Any]:
    try:
        undone = apply.undo_change(change_id, force=bool(force))
    except apply.UndoRefused as err:
        # Refused, not clobbered: restoring would overwrite what the user did
        # themselves since.
        return {"ok": False, "needsConfirmation": True, "error": str(err),
                "live": err.live, "expected": err.expected}
    except (KeyError, ValueError) as err:
        return {"ok": False, "error": str(err)}
    return {"ok": True, "undone": undone["target"]}


SPECS = [
    CapabilitySpec(
        id="builtin.record_lesson", name="record_lesson",
        description=("Record a lasting preference the user has plainly taught you — "
                     "\"next time, just…\", \"I always want…\", \"don't do that again\". "
                     "Their teaching is the trigger. A one-off request for this moment "
                     "is NOT a lasting preference."),
        input_schema={"type": "object", "properties": {
            "text": {"type": "string", "description": "The preference, in one clear line."},
            "scope": {"type": "string",
                      "description": '"general", or "tool:<name>" if it is about one ability.'}},
            "required": ["text"]},
        # An observation, not a change: nothing about how Jarvis behaves moves
        # because of this on its own.
        risk=Risk.LOW, handler=_record_lesson, timeout_s=10.0, tags=frozenset({"meta"}),
    ),
    CapabilitySpec(
        id="builtin.suggest_improvement", name="suggest_improvement",
        description=("Propose a concrete change to how you work — when the user asks for "
                     "one, or rarely, when you notice something specific worth proposing."),
        input_schema={"type": "object", "properties": {
            "title": {"type": "string"},
            "rationale": {"type": "string"},
            "kind": {"type": "string", "description": '"rule", "setting", "skill" or "code".'},
            "text": {"type": "string", "description": "The rule itself, if it is a rule."}},
            "required": ["title"]},
        risk=Risk.LOW, handler=_suggest, timeout_s=10.0, tags=frozenset({"meta"}),
    ),
    CapabilitySpec(
        id="builtin.review_improvements", name="review_improvements",
        description=("Show what you have learned, changed, or have waiting — always the "
                     "real record, never an answer from impression."),
        input_schema={"type": "object", "properties": {}, "required": []},
        risk=Risk.LOW, handler=_review, timeout_s=10.0, tags=frozenset({"meta"}),
    ),
    CapabilitySpec(
        id="builtin.undo_improvement", name="undo_improvement",
        description="Undo a change to how you work, when the user asks.",
        input_schema={"type": "object", "properties": {
            "change_id": {"type": "string"},
            "force": {"type": "boolean",
                      "description": "Only after the user has been shown that it changed "
                                     "since and said to restore it anyway."}},
            "required": ["change_id"]},
        risk=Risk.MEDIUM, handler=_undo,
        summarize=lambda args: f"Undo change {args.get('change_id')}?",
        timeout_s=10.0, tags=frozenset({"meta"}),
    ),
]
