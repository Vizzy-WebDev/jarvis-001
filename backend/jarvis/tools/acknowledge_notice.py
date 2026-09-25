"""Marking a waiting notice as actually delivered.

A notice is shown to the model on a turn the user started, but is NOT marked
delivered by being shown: a turn that fails before the model replies would
otherwise lose it silently. This is the action that says it genuinely reached
the person — which is why it is a tool call rather than a side effect of
rendering the prompt.

Only for notices with no other resolving action of their own. Background work
has `check_on_work` and `stop_working_on`, which resolve their rows by doing
something about them.
"""

from __future__ import annotations

from typing import Any

from ..capabilities import CapabilitySpec, Risk


def _run(notice_id: int | str = 0) -> dict[str, Any]:
    from ..heartbeat import outbox

    try:
        entry_id = int(notice_id)
    except (TypeError, ValueError):
        return {"ok": False, "error": "That is not a notice number."}

    entry = outbox.get(entry_id)
    if entry is None:
        return {"ok": False, "error": "There is no notice with that number."}
    if entry["deliveredAt"]:
        return {"ok": True, "alreadyDone": True,
                "note": "That one was already dealt with — don't mention it again."}
    if entry["source"] == "job":
        # Its own actions resolve it by doing something about it; marking it
        # delivered here would drop it while the job is still waiting.
        return {"ok": False,
                "error": "That one is about background work — use check_on_work or "
                         "stop_working_on instead."}

    outbox.mark_delivered(entry_id)
    return {"ok": True, "note": "Noted as passed on. Don't mention it again unless asked."}


SPEC = CapabilitySpec(
    id="builtin.acknowledge_notice",
    name="acknowledge_notice",
    description=("Mark a waiting notice as passed on, once you have actually mentioned it to "
                 "the user. Call it with the notice number you were shown. Only for notices — "
                 "anything about background work is resolved with check_on_work or "
                 "stop_working_on instead."),
    input_schema={"type": "object", "properties": {
        "notice_id": {"type": "integer", "description": "The number shown with the notice."}},
        "required": ["notice_id"]},
    risk=Risk.LOW,
    handler=_run,
    timeout_s=10.0,
    tags=frozenset({"core", "meta"}),
)
