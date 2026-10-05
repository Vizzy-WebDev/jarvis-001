"""The only thing that changes anything, and the only thing that undoes it.

**Jarvis never edits its own code — structurally, not by convention.** This
module writes exactly two things: rows in the improvement tables, and keys in
preferences. It cannot write a file under the source tree because it never opens
one, and it refuses outright for any proposal kind other than a rule or a
setting. A proposal that needs real code produces a BRIEF for a human's coding
assistant instead; generating that brief IS the approval action for those kinds.

**Undo refuses rather than clobbers.** It compares the live value against what
the change actually set. If the user has since changed it themselves — muted the
rule, edited the preference — restoring the old value would silently overwrite
their own later decision, so it says so and stops unless told to proceed anyway.
An undo is itself never undoable: bringing something back means approving it
again, which is what keeps every before/after pair in the log honest.
"""

from __future__ import annotations

from typing import Any

from ..prefs import get_prefs, set_prefs
from . import store
from .policy import AUTO_APPLY, APPLIABLE_KINDS, decide


class UndoRefused(RuntimeError):
    """The live value is not what this change set. Carries what it found."""

    def __init__(self, message: str, *, live: Any, expected: Any):
        super().__init__(message)
        self.live = live
        self.expected = expected


def apply_proposal(proposal_id: str, *, approved_by: str = "user") -> dict[str, Any]:
    """Make the change a proposal describes. Refuses anything that is not a rule
    or a setting, whatever the caller believes it has approved."""
    proposal = store.get_proposal(proposal_id)
    if proposal is None:
        raise KeyError(f"No such proposal: {proposal_id}")
    if proposal["kind"] not in APPLIABLE_KINDS:
        raise ValueError(
            f"A {proposal['kind']} proposal is never applied automatically — it needs "
            "real work by a person.")

    payload = proposal.get("payload") or {}
    if proposal["kind"] == "rule":
        rule = store.create_rule(text=str(payload.get("text") or proposal["title"]),
                                 scope=str(payload.get("scope") or "general"),
                                 source_proposal_id=proposal_id)
        change = store.record_change(kind="rule", target=rule["id"], before=None,
                                     after={"text": rule["text"], "active": True},
                                     reason=f"approved by {approved_by}",
                                     proposal_id=proposal_id)
    else:
        key = str(payload.get("key") or "")
        if not key:
            raise ValueError("A setting proposal has to say which setting.")
        before = get_prefs().get(key)
        after = payload.get("value")
        set_prefs({key: after})
        change = store.record_change(kind="setting", target=key, before=before, after=after,
                                     reason=f"approved by {approved_by}",
                                     proposal_id=proposal_id)

    store.set_proposal_status(proposal_id, "approved")
    return change


def auto_apply_if_allowed(proposal_id: str) -> dict[str, Any] | None:
    """Apply only if the policy says so. The policy is asked here rather than
    trusted from the caller, so a caller that forgot to check cannot bypass it."""
    proposal = store.get_proposal(proposal_id)
    if proposal is None:
        return None
    if decide(proposal) != AUTO_APPLY:
        return None
    return apply_proposal(proposal_id, approved_by="Jarvis (within your settings)")


def undo_change(change_id: int | str, *, force: bool = False) -> dict[str, Any]:
    change = store.get_change(change_id)
    if change is None:
        raise KeyError(f"No such change: {change_id}")
    if change.get("undone_at"):
        raise ValueError("That has already been undone.")
    if change["kind"] == "undo":
        # Bringing something back means approving it again, not reversing a
        # reversal — which is what keeps the log's before/after pairs honest.
        raise ValueError("An undo can't itself be undone.")

    if change["kind"] == "rule":
        rule = store.get_rule(change["target"])
        if rule is None:
            raise ValueError("That rule is no longer here.")
        live = {"text": rule["text"], "active": bool(rule["active"])}
        if not force and live != (change.get("after") or {}):
            raise UndoRefused(
                "That rule has changed since — undoing would overwrite what you did to it.",
                live=live, expected=change.get("after"))
        store.archive_rule(rule["id"])
        store.set_rule_active(rule["id"], False)
    else:
        key = change["target"]
        live = get_prefs().get(key)
        if not force and live != change.get("after"):
            raise UndoRefused(
                f"'{key}' has changed since — undoing would overwrite what you set.",
                live=live, expected=change.get("after"))
        set_prefs({key: change.get("before")})

    store.mark_change_undone(change_id)
    return store.record_change(kind="undo", target=change["target"],
                               before=change.get("after"), after=change.get("before"),
                               reason=f"undo of {change_id}")
