"""Checking what Jarvis actually knows about itself, and recording a goal.

`check_myself` is the PULL path. The push path can only warn about something this
turn has already used; this is what catches it before the first use — which is
why the system prompt tells the model to consult it before making a claim about
its own reliability rather than answering from impression.

Every answer is a snapshot, saved as it was returned, so a claim built on it can
be checked afterwards against what was really available.
"""

from __future__ import annotations

from typing import Any

from ..capabilities import CapabilitySpec, Risk
from ..self import model, store

DIMENSIONS = tuple(model.BUILDERS)


def _check(about: list[str] | None = None, dimensions: list[str] | None = None,
           ) -> dict[str, Any]:
    from ..session import get_active_session_id

    wanted = [d for d in (dimensions or ["can_do"]) if d in DIMENSIONS] or ["can_do"]
    session_id = get_active_session_id()
    answer = model.build(wanted, about=about, session_id=session_id)
    snapshot_id = store.save_snapshot(conversation_id=session_id, snapshot=answer)
    return {"ok": True, "snapshotId": snapshot_id, **answer,
            "note": ("These are real counts and live settings. Where something says "
                     "no_track_record, say so plainly rather than estimating.")}


def _track_goal(goal: str = "") -> dict[str, Any]:
    from .. import conversation
    from ..session import get_active_session_id

    text = (goal or "").strip()
    if not text:
        return {"ok": False, "error": "There's no goal to record."}
    session_id = get_active_session_id()
    # The user's own words at the time, so drift can later be judged against what
    # was actually said rather than against this paraphrase of it.
    latest = next((m.get("text") for m in reversed(conversation.get_messages(session_id))
                   if m.get("role") == "user" and m.get("text")), None)
    recorded = store.declare_goal(scope_kind="conversation", scope_ref=session_id,
                                  goal_text=text, source_turn_text=latest)
    return {"ok": True, "id": recorded["id"],
            "note": "Recorded as what I understood the goal to be. Don't mention this.",
            "spoken_hint": "Say nothing about this — it is a background note."}


SPECS = [
    CapabilitySpec(
        id="builtin.check_myself", name="check_myself",
        description=("Check what you actually know about yourself before claiming it — how "
                     "reliable you have been at something, what you are doing right now, "
                     "what is genuinely your call, or how you tend to fail. Never answer "
                     "one of those from impression; if it comes back with no track record, "
                     "say that plainly instead of estimating."),
        input_schema={"type": "object", "properties": {
            "dimensions": {"type": "array",
                           "description": 'Any of "can_do", "failure_modes", '
                                          '"how_it_behaves", "doing_now", "whats_its_call".'},
            "about": {"type": "array",
                      "description": "Capability names to check reliability for."}},
            "required": []},
        risk=Risk.LOW, handler=_check, timeout_s=15.0, tags=frozenset({"core", "meta"}),
    ),
    CapabilitySpec(
        id="builtin.track_goal", name="track_goal",
        description=("Record what you understand this conversation is actually trying to "
                     "achieve, once it becomes clear — so you can notice later if you have "
                     "drifted. Not for a quick one-off question. Never mention calling it."),
        input_schema={"type": "object", "properties": {
            "goal": {"type": "string", "description": "The goal, in one line."}},
            "required": ["goal"]},
        risk=Risk.LOW, handler=_track_goal, timeout_s=10.0, tags=frozenset({"meta"}),
    ),
]
