"""Is the scheduler actually firing?

Only meaningful when the scheduler is switched on: while it is deliberately off
before cutover, an overdue task is expected, and reporting it would be reporting
a decision as a fault.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..registry import Check

#: Well past the tick interval, so a task merely waiting for the next tick — or
#: for a machine that was asleep — is never reported.
OVERDUE_GRACE = timedelta(minutes=30)


def probe() -> dict[str, Any]:
    from ....scheduler import engine, task_store

    if not engine.is_enabled():
        return {"ok": True}

    now = datetime.now(timezone.utc)
    late = []
    for task in task_store.list_tasks():
        if not task.get("enabled", True):
            continue
        due = task.get("nextRunAt")
        if not due:
            continue
        when = datetime.fromisoformat(str(due).replace("Z", "+00:00"))
        if now - when > OVERDUE_GRACE:
            late.append(task.get("title") or task.get("id"))
    if late:
        return {"ok": False,
                "detail": (f"{len(late)} scheduled task(s) are long past due while the "
                           f"scheduler is switched on: {', '.join(str(t) for t in late[:3])}")}
    return {"ok": True}


CHECK = Check(id="scheduler", probe=probe)
