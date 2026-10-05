"""A scheduled prompt run as durable work (`jarvis/durable.py`): one round, one turn.

Before this, a run that Jarvis stopped part-way through was simply gone — and a one-off task
is switched off before it runs (`engine.tick` advances the schedule first), so it was gone for
good. Now the run's turn is one round of durable work: whatever it finished is on record, and
at the next start the run is picked back up ONCE, finishes, and is recorded as "resumed after
Jarvis restarted". A run interrupted a second time is recorded as not tried again rather than
looping on every start.

Unchanged: the run is `Autonomy.PRE_CONSENTED` (a HIGH-risk call still parks for a person), its
session is its own and never bound to chat history, and a scheduled run done BY a specialist
still goes through `agents/runner.run_agent`.
"""

from __future__ import annotations

import logging
import threading
import uuid
from typing import Any, Iterator

from .. import durable
from ..events.bus import EventBus

logger = logging.getLogger(__name__)

KIND = "task_run"

#: Restarts a run survives: picked back up once; interrupted again, it is not tried again.
MAX_RECOVERIES = 1

#: The bus a run's own events go to (its hooks run inside saved steps).
_buses: dict[str, EventBus] = {}


def run_prompt(task: dict[str, Any], action: dict[str, Any], text: str, *, late: bool = False,
               event_bus: EventBus | None = None) -> dict[str, Any]:
    work_id = f"taskrun-{uuid.uuid4().hex[:12]}"
    meta = {"taskId": task["id"], "late": late, "session": f"task:{task['id']}:{work_id[-8:]}",
            # As it was when the run started — used only if the task is gone by the time a
            # round runs (each round reads the task afresh, so its connectors stay current).
            "task": {"id": task["id"], "title": task.get("title"), "action": action}}
    if event_bus is not None:
        _buses[work_id] = event_bus
    try:
        reached = durable.advance(work_id, KIND, text, meta=meta)
    finally:
        _buses.pop(work_id, None)
    return reached.get("result") or {"ok": False, "summary": "", "error": "It did not finish."}


def _session_for(work_id: str) -> str:
    work = durable.get(work_id) or {}
    return (work.get("meta") or {}).get("session") or f"task:{work_id}"


def _turn(work_id: str, text: str, spec: Any, cancel: threading.Event) -> Iterator[Any]:
    from ..assembly import get_orchestrator
    from ..orchestrator import Failed
    from .engine import prompt_request
    from .task_store import get_task

    meta = (durable.get(work_id) or {}).get("meta") or {}
    task = get_task(meta.get("taskId") or "") or meta.get("task")
    if not task:
        yield Failed("This scheduled task no longer exists.")
        return
    request = prompt_request(task, text, _session_for(work_id), spec)
    yield from get_orchestrator().run_turn(request, cancel)


def _result_of(outcome: dict[str, Any]) -> dict[str, Any]:
    """How the run ended, in the shape the run history records."""
    approval = outcome.get("approval")
    if approval:
        return {"ok": False, "summary": outcome.get("text") or "",
                "awaitingApproval": approval.get("id"),
                "error": f"{approval.get('capability')} needs your go-ahead before this can "
                         "finish."}
    if outcome.get("outcome") != "finished":
        return {"ok": False, "summary": outcome.get("text") or "",
                "error": outcome.get("error") or "It was stopped before it finished."}
    return {"ok": True, "summary": outcome.get("text") or "", "modelId": outcome.get("modelId")}


def _finish(work_id: str, outcome: dict[str, Any]) -> dict[str, Any]:
    from .engine import complete_run
    from .task_store import get_task

    work = durable.get(work_id) or {}
    meta = work.get("meta") or {}
    result = _result_of(outcome)
    task = get_task(meta.get("taskId") or "")
    if task is None:
        return {**result, "recorded": True}
    return complete_run(task, result, late=bool(meta.get("late")), event_bus=_buses.get(work_id),
                        resumed=(work.get("recoveries") or 0) > 0, work_id=work_id)


def recover_interrupted() -> list[dict[str, Any]]:
    """At startup: pick back up each prompt run a restart interrupted, on its own thread.
    A run already picked up once is recorded as not tried again instead."""
    from ..background import run_in_background

    if not _has_store():
        return []
    found = durable.with_status(KIND, "running")
    for work in found:
        run_in_background(lambda work_id=work["id"]: _recover(work_id),
                          name=f"task-run-recovery-{work['id']}")
    return found


def _has_store() -> bool:
    from ..store import data_dir

    return (data_dir() / durable.FILE_NAME).exists()


def _recover(work_id: str) -> None:
    from .engine import complete_run
    from .task_store import get_task

    reached = durable.recover(work_id, max_recoveries=MAX_RECOVERIES)
    if not reached or reached.get("state") not in ("abandoned", "unsure"):
        return
    work = durable.get(work_id) or {}
    meta = work.get("meta") or {}
    task = get_task(meta.get("taskId") or "")
    result = {"ok": False, "summary": "", "error": _why_not(reached)}
    if task is not None:
        result = complete_run(task, result, late=bool(meta.get("late")), work_id=work_id)
    durable.abandon(work_id, result)


def _why_not(reached: dict[str, Any]) -> str:
    if reached.get("state") == "unsure":
        names = ", ".join(sorted({a["capability"] for a in reached.get("actions") or []}))
        return (f"Jarvis stopped in the middle of {names}, which reaches outside Jarvis, so "
                "it may or may not have happened. It was not repeated — please check.")
    return "Jarvis stopped part-way through this run twice, so it was not tried again."


durable.register(durable.Kind(name=KIND, session_for=_session_for, turn=_turn, finish=_finish))
