"""A worker asking for its own job to be broken into separate pieces.

Only a background job's own turn can call this — `ctx.session_id` has to be a
`job:` session, and the tool is declared to nothing else (see the `job` tag and
`orchestrator/pipeline.py`'s declaration filter). A live conversation has
`work_in_background` for starting work; this is the worker's own voice, saying
that what it was given is really three independent things.

**Splitting is judged, never automatic.** One model call asks whether the split
is PROPORTIONATE — not whether the goal is worth doing, which was settled when
the job was admitted. Anything short of an explicit yes is a no: no model, an
unreadable answer and genuine uncertainty all mean "keep working on it as one
job", which costs a slower job rather than three jobs nobody wanted.

**The depth ceiling is structural, not a rule to remember.** Every approved piece
takes the ROOT ancestor as its parent, resolved in one hop — a level-2 job
already carries the root in its own `parentId`, so its pieces become PEERS under
that same root rather than children of itself. A tree cannot exceed depth 2 no
matter how many times anything asks.
"""

from __future__ import annotations

import logging
from typing import Any

from ..capabilities import CapabilitySpec, Risk
from ..jobs import job_store
from ..jobs.orchestrator import AtCapacity, active_job_limit, admit

logger = logging.getLogger(__name__)

#: More pieces than this is not a split, it is a different plan.
MAX_PIECES = 5
MIN_PIECES = 2

JUDGE_SYSTEM = (
    "You are the Orchestrator of a background task system, judging whether a worker's "
    "request to split its own job into separate pieces is proportionate — NOT whether the "
    "underlying goal is a good idea, which was decided when the job was created. Approve "
    "only if the pieces are genuinely independent and doing them separately is a real "
    "improvement on one job handling all of it. Deny if the split is unnecessary, if the "
    "pieces overlap, or if there are more of them than the goal calls for. If you are not "
    "genuinely sure, deny: being unsure is itself a reason not to approve."
)


def _pieces_from(raw: Any) -> list[dict[str, str]]:
    pieces = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        goal = str(entry.get("goal") or "").strip()
        if not goal:
            continue
        title = str(entry.get("title") or "").strip()[:80] or goal[:80]
        pieces.append({"title": title, "goal": goal})
    return pieces


def _judge(job: dict[str, Any], reason: str, pieces: list[dict[str, str]]) -> tuple[bool, str]:
    from ..ai import ask_model

    listed = "\n".join(f"{i}. {p['goal']}" for i, p in enumerate(pieces, start=1))
    reply = ask_model(
        f"Original job goal: {job['goal']}\n"
        f"Reason given for splitting: {reason or '(none given)'}\n"
        f"Proposed pieces:\n{listed}\n\n"
        'Answer with JSON only: {"approved": true or false, "reason": "one sentence"}.',
        system=JUDGE_SYSTEM, want_json=True, background=True)
    if not reply.ok or not isinstance(reply.data, dict):
        # Silence is not consent. A judgment that could not be made is a denial.
        return False, "I couldn't judge whether splitting this up is worth it, so I kept going."
    approved = reply.data.get("approved") is True
    return approved, str(reply.data.get("reason") or "").strip()


def _run(pieces: Any = None, reason: str = "", ctx: Any = None) -> dict[str, Any]:
    session_id = getattr(ctx, "session_id", "") or ""
    if not session_id.startswith("job:"):
        return {"ok": False,
                "error": "request_job_split can only be called while working on a "
                         "background job."}
    job_id = session_id[len("job:"):]
    job = job_store.get_job(job_id)
    if job is None:
        return {"ok": False, "error": "Unknown job."}

    wanted = _pieces_from(pieces)
    # Everything cheap first: a request that cannot be approved must not cost a
    # model call to refuse, which is the same discipline admission already uses.
    if len(wanted) < MIN_PIECES:
        return {"ok": True, "approved": False,
                "reason": "At least two distinct pieces are needed to justify splitting."}
    if len(wanted) > MAX_PIECES:
        return {"ok": True, "approved": False,
                "reason": f"Too many pieces ({len(wanted)}, at most {MAX_PIECES}) for "
                          "one split."}

    # This job does not count against the room its own pieces need: if the split
    # is approved it finishes, replaced by them. Counting it would mean a limit
    # of two could never approve a split at all.
    others = [j for j in job_store.list_active_jobs() if j["id"] != job_id]
    room = active_job_limit() - len(others)
    if room < len(wanted):
        return {"ok": True, "approved": False,
                "reason": f"There isn't room for {len(wanted)} more jobs right now."}

    approved, judged = _judge(job, str(reason or "").strip(), wanted)
    if not approved:
        return {"ok": True, "approved": False,
                "reason": judged or "Splitting this up wouldn't be an improvement."}

    # The root in one hop — see the module docstring on why this is the whole
    # depth ceiling.
    root = job.get("parentId") or job["id"]
    job_store.append_trace(job_id, phase="intent", effect="workspace", kind="decision",
                           summary=f"splitting into {len(wanted)} piece(s)",
                           detail={"root": root, "pieces": [p["goal"] for p in wanted]})

    # This job finishes BEFORE its pieces are created, not after: it is being
    # replaced by them, and while it still counts as active it holds a slot its
    # own pieces need — which showed up immediately as a two-piece split
    # producing one piece. The intent row above is what makes this safe: a crash
    # in the middle leaves the split on the record rather than a job that
    # finished for no visible reason.
    previous_status = job.get("status") or "running"
    job_store.update_job(job_id, {"status": "done", "result": "splitting into pieces"})

    created: list[str] = []
    for piece in wanted:
        try:
            # `generic` deliberately: no second admission call per piece, so the
            # whole split costs one judgment call.
            piece_job = admit(title=piece["title"], goal=piece["goal"], kind="generic",
                              conversation_id=job.get("conversationId"), parent_id=root)
        except AtCapacity:
            # Room ran out part-way. Stop at what was actually created rather
            # than pushing past a limit that exists for a reason.
            break
        created.append(piece_job["id"])

    if not created:
        # Nothing started, so nothing was split. Finishing the job here would
        # mean reporting work as done that no one is doing.
        job_store.update_job(job_id, {"status": previous_status, "result": None})
        job_store.append_trace(job_id, phase="outcome", effect="read", kind="decision",
                               summary="split approved but nothing could be started")
        return {"ok": True, "approved": False,
                "reason": "There wasn't room to start any of the pieces, so I've carried on "
                          "with the job as it is."}

    job_store.append_trace(job_id, phase="outcome", effect="workspace", kind="decision",
                           summary=f"split approved — created {len(created)} piece(s)",
                           detail={"reason": judged, "jobIds": created})
    job_store.update_job(job_id, {
        "status": "done",
        "result": f"Split into {len(created)} separate job(s): {', '.join(created)}",
    })
    # Tier 3: worth recording, never worth interrupting for.
    job_store.add_outbox(tier=3, job_id=job_id, reason="finished",
                         summary=f'"{job["title"]}" was split into {len(created)} job(s).')

    return {"ok": True, "approved": True, "newJobIds": created,
            "note": f"Approved — this job's work is now {len(created)} separate job(s). "
                    "Stop here rather than carrying on with the original goal."}


SPEC = CapabilitySpec(
    id="builtin.request_job_split",
    name="request_job_split",
    description=("Call this if the background job you are working on would genuinely go "
                 "better as separate, independent pieces of work rather than one — three "
                 "distinct searches instead of one broad one, say. It is not automatic: "
                 "give a reason and describe each piece as its own complete, self-contained "
                 "goal, and the Orchestrator judges whether splitting is proportionate. If "
                 "it is approved, each piece becomes its own background job and THIS job is "
                 "done — stop rather than also carrying on with the original goal. If it is "
                 "denied, keep working on the goal as it stands."),
    input_schema={"type": "object", "properties": {
        "reason": {"type": "string",
                   "description": "Why splitting this up would genuinely help."},
        "pieces": {
            "type": "array", "minItems": MIN_PIECES,
            "description": "Each independent piece of work, at least two.",
            "items": {"type": "object", "properties": {
                "title": {"type": "string"},
                "goal": {"type": "string",
                         "description": "A complete, self-contained goal for this one "
                                        "piece."}},
                "required": ["goal"]}}},
        "required": ["pieces"]},
    # It creates real background work, but only ever inside a job the user
    # already agreed to, and the judgment is the gate. Nobody is present to ask.
    risk=Risk.LOW,
    handler=_run,
    wants_context=True,
    timeout_s=60.0,
    # `job` declares it to a background worker's own turn and to nothing else
    # (see orchestrator/pipeline.py's declaration filter). `core` keeps it past
    # the declaration budget once it is there: on a job's own turn this IS core,
    # and a tool that is filtered out of every turn is a tool that does not
    # exist.
    tags=frozenset({"job", "core"}),
)
