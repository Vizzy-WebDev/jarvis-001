"""Feeding Self-Improvement's capture step from real job/task activity.

`improvement/capture.py`'s `record_job_outcome()`/`record_task_outcome()` are
complete and correct — zero model calls, safe to call on every terminal
status — but nothing called them: not `jobs/worker.py`, not
`jobs/orchestrator.py`, not `scheduler/engine.py`. So the whole
`capture -> reflect -> synthesize -> apply` pipeline never ran on real
activity, only on the explicit `record_lesson`/`suggest_improvement` tools a
user calls directly. This is what actually connects it, the same way
`_record_tool_outcome` in `recording.py` already connects Self-Model's own
recorder to the bus.

**Re-reads the job at event time rather than trusting the event payload.**
`JOB_COMPLETED`/`JOB_UPDATED` carry only `{id, status, title}` — deliberately
thin, same reasoning `connectors/capabilities.py`'s `_dispatch()` gives for
re-reading a connector rather than closing over a stale copy: acting on
whatever the payload happened to carry is exactly the kind of staleness bug
that only shows up once it matters.
"""

from __future__ import annotations

import logging

from ..events.bus import Event

logger = logging.getLogger(__name__)


def _record_job_outcome(event: Event) -> None:
    from ..improvement import capture
    from ..jobs import job_store

    job_id = event.payload.get("id")
    if not job_id:
        return
    job = job_store.get_job(job_id)
    if job is None:
        return
    trace = job_store.get_trace(job_id)
    capture.record_job_outcome(job, trace)


def _check_for_correction(event: Event) -> None:
    """Subscribes to ASSISTANT_INPUT rather than the turn loop calling this
    directly — `orchestrator/pipeline.py`'s own header comment states the
    invariant this protects: it deliberately does not import improvement
    capture (or cost tracking, the self-model, personality, tracing), all of
    which subscribe to the bus instead. `test_architecture.py` asserts this."""
    from ..improvement.capture import note_correction
    from ..policy import Surface

    if event.payload.get("surface") in (Surface.JOB.value, Surface.SCHEDULED.value):
        return  # nobody's correcting anything on a job's or a task's own turn
    note_correction(event.payload.get("text") or "")


def _record_task_outcome(run: dict) -> None:
    """Called directly by scheduler.engine.run_task_now() — a scheduled task
    run has no event of its own to subscribe to (unlike jobs, which already
    publish JOB_COMPLETED/JOB_UPDATED), so this is a plain function rather
    than a bus subscriber."""
    from ..improvement import capture
    from ..scheduler.task_store import get_task

    task = get_task(run.get("taskId")) if run.get("taskId") else None
    capture.record_task_outcome(run, task)
