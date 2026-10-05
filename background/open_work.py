"""What Jarvis has going for the person right now — a read-only snapshot, as plain data.

Its own module, outside the turn loop: gathering it reads the jobs, specialist-run and watch
stores, and the loop has to stay free of those (it never imports the agents package, and
`orchestrator/context.py` only reads what this hands it). `prompt.open_work_section` turns the
snapshot into the few lines Jarvis is shown.

A READ and nothing else. Each part is gathered on its own, so one store failing costs only its own
lines, and the whole thing never raises: a turn must never fail because of it.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: How many specialist runs are looked at (newest first) when deciding which are still going.
RUNS_LOOKED_AT = 30


def snapshot() -> dict[str, Any]:
    work: dict[str, Any] = {"jobs": [], "runs": [], "watches": []}
    try:
        from .jobs import job_store

        work["jobs"] = [{k: j.get(k) for k in ("title", "status", "currentStep", "progress",
                                               "error")}
                        for j in job_store.list_active_jobs()]
    except Exception:  # noqa: BLE001
        logger.exception("could not read the jobs for the open-work section")
    try:
        from .agents import store as agent_store

        names = {a["id"]: a["name"] for a in agent_store.list_agents(include_disabled=True)}
        work["runs"] = [
            {"agent": names.get(r["agentId"], r["agentId"]), "task": str(r["task"])[:100]}
            for r in agent_store.list_runs(limit=RUNS_LOOKED_AT)
            # Only runs someone asked for and is still waiting on: a nested run belongs to its
            # root, and a job's run is already listed as that job.
            if r["status"] == "running" and not r.get("parentRunId") and not r.get("jobId")]
    except Exception:  # noqa: BLE001
        logger.exception("could not read the specialist runs for the open-work section")
    try:
        from .monitor import store as monitor_store

        work["watches"] = [str(m.get("description") or "something")
                           for m in monitor_store.list_watching()]
    except Exception:  # noqa: BLE001
        logger.exception("could not read the watches for the open-work section")
    return work
