"""Checking what Jarvis actually knows about itself.

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


SPECS = [
    CapabilitySpec(
        id="builtin.check_myself", name="check_myself",
        description=("Check what you actually know about yourself before claiming it — how "
                     "reliable you have been at something, what you are doing right now, "
                     "what is genuinely your call, or how you tend to fail. Never answer "
                     "one of those from impression; if it comes back with no track record, "
                     "say that plainly instead of estimating."),
        input_schema={"type": "object", "properties": {
            # An array says what it holds. Some providers refuse one that doesn't
            # (Gemini: "items: missing field"), which fails every turn that declares it.
            "dimensions": {"type": "array", "items": {"type": "string"},
                           "description": 'Any of "can_do", "failure_modes", '
                                          '"how_it_behaves", "doing_now", "whats_its_call".'},
            "about": {"type": "array", "items": {"type": "string"},
                      "description": "Capability names to check reliability for."}},
            "required": []},
        risk=Risk.LOW, handler=_check, timeout_s=15.0, tags=frozenset({"core", "meta"}),
    ),
]
