"""Is background work being supervised at all?

Not "is a job failing" — a job that fails is Jobs' own business and has its own
recovery. This asks whether the SUPERVISOR is running: a job left in `running`
with a heartbeat far older than the hang timeout should already have been
recovered, and the fact that it has not been is a fault in this build, not in
the work.

The remedy is one supervision pass, which is exactly what should have happened
on its own.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..registry import Check

#: Comfortably past the point where the supervisor should have acted, so a job
#: merely between heartbeats is never reported.
STALE_MULTIPLIER = 3


def probe() -> dict[str, Any]:
    from ....jobs import job_store
    from ....jobs.orchestrator import HANG_TIMEOUT_MS

    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    limit = HANG_TIMEOUT_MS * STALE_MULTIPLIER
    stale = []
    for job in job_store.list_active_jobs():
        if job.get("status") != "running":
            continue
        beat = job.get("heartbeatAt") or job.get("startedAt")
        if not beat:
            continue
        age = now_ms - datetime.fromisoformat(str(beat).replace("Z", "+00:00")).timestamp() * 1000
        if age > limit:
            stale.append(job["id"])
    if stale:
        return {"ok": False,
                "detail": (f"{len(stale)} background job(s) have been running with no sign of "
                           f"life for far longer than the recovery timeout, which means "
                           f"supervision is not running: {', '.join(stale[:3])}")}
    return {"ok": True}


def remedy() -> None:
    from ....jobs import orchestrator

    orchestrator.supervise()


CHECK = Check(id="jobs", probe=probe, remedy=remedy)
