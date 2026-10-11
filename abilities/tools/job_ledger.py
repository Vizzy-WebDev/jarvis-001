"""A background worker keeping its own plan and findings — `update_plan`.

Only a job's own turn can call it (the `job` tag; `ctx.session_id` has to be a `job:` session).
What it writes is the job's ledger (`jobs/objective.py`), which is put back in front of the
worker at the start of every round — so a plan made in round one is still the plan in round
five, after the transcript that held it has scrolled away or a restart has replayed the round.

One tool, whole-plan replacement: the worker restates its steps with each one's status. Small
models handle "here is the list as it now stands" far more reliably than add/edit/remove
calls that have to agree with a list they can no longer see.
"""

from __future__ import annotations

from typing import Any

from ..capabilities import CapabilitySpec, Risk
from ..jobs import objective

STATUSES = list(objective.STEP_STATUSES)


def _run(steps: Any = None, found: Any = None, open_questions: Any = None,
         ctx: Any = None) -> dict[str, Any]:
    session_id = getattr(ctx, "session_id", "") or ""
    if not session_id.startswith(objective.SESSION_PREFIX):
        return {"ok": False, "error": "update_plan can only be called while working on a "
                                      "background job."}
    ledger = objective.update_ledger(session_id[len(objective.SESSION_PREFIX):], steps=steps,
                                     found=found, open_questions=open_questions)
    if ledger is None:
        return {"ok": False, "error": "Unknown job."}
    left = [s["text"] for s in ledger["steps"] if s["status"] in ("todo", "doing")]
    return {"ok": True, "steps": len(ledger["steps"]), "stillToDo": left,
            "found": len(ledger["found"]),
            **({"note": f"{ledger['dropped']} of your oldest findings were dropped to make room "
                        "— merge findings into fewer, fuller lines."} if ledger.get("dropped") else {})}


SPEC = CapabilitySpec(
    id="builtin.update_plan", name="update_plan",
    description=("Keep your plan and what you have found, for the work you are doing. Call it "
                 "first with the steps the work needs, then whenever a step is finished or you "
                 "find something worth keeping (a figure, a name, a link). It is shown back to "
                 "you at the start of every round; your earlier messages may not be."),
    input_schema={"type": "object", "properties": {
        "steps": {"type": "array",
                  "description": "The WHOLE plan as it now stands, in order.",
                  "items": {"type": "object", "properties": {
                      "text": {"type": "string", "description": "The step."},
                      "status": {"type": "string", "enum": STATUSES},
                      "note": {"type": "string", "description": "Optional: what came of it."}},
                      "required": ["text", "status"]}},
        "found": {"type": "array", "items": {"type": "string"},
                  "description": "New findings to add to what you have kept so far."},
        "open_questions": {"type": "array", "items": {"type": "string"},
                           "description": "What is still unknown or blocked (replaces the last list)."}},
        "required": []},
    # It writes only the job's own notes.
    risk=Risk.LOW, handler=_run, wants_context=True, timeout_s=10.0,
    tags=frozenset({"job", "core"}),
)
