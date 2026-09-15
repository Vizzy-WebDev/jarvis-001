"""Scheduled tasks over HTTP.

A thin surface over `scheduler/task_store.py`, which already holds every rule
about what a task is — recurrence, next-run computation, run history and its
cap. Nothing here decides anything; a route that started computing a next run
would be a second answer to a question the store already answers.

**The one piece of judgment that does live here** is the same one the Node
routes carry, and for the same reason: a `skill` action naming something that
does not exist is refused at the door. `task_store.py` is a leaf and must never
import the capability registry (the circular-import rule in the root CLAUDE.md),
so the check belongs at the edge, where importing it is safe.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from ..scheduler import task_store
from ..scheduler.recurrence import describe

router = APIRouter(prefix="/api")


def _unknown_skill(action: Any) -> str | None:
    """The named skill, if this action names one that does not exist."""
    if not isinstance(action, dict) or action.get("type") != "skill":
        return None
    name = str(action.get("skillName") or "")

    from ..assembly import get_registry

    return None if get_registry().get(name) else name


@router.get("/tasks")
def listed() -> dict[str, Any]:
    """Every task, plus the plain-English sentence for each one's schedule.

    The sentence is a SIBLING map rather than a field inside each task, so the
    recorded task shape stays byte-identical and the addition is a single
    top-level key the contract harness can be told about and then enforce (see
    `ADDED_KEYS`). It comes from `recurrence.describe()` — the same function the
    spoken read-back uses — rather than being re-derived in TypeScript, which
    would be a second, worse copy of the scheduling vocabulary.
    """
    tasks = task_store.list_tasks()
    return {"tasks": tasks, "descriptions": {t["id"]: describe(t.get("recurrence") or {})
                                             for t in tasks}}


@router.get("/tasks/{task_id}")
def one(task_id: str):
    task = task_store.get_task(task_id)
    if task is None:
        return JSONResponse({"ok": False, "error": "Unknown task."}, status_code=404)
    # The screen shows a task beside what it has actually been doing, so the
    # detail view is one request rather than two.
    return {"ok": True, "task": task, "runs": task_store.list_runs(task_id),
            "description": describe(task.get("recurrence") or {})}


@router.post("/tasks")
def create(body: dict[str, Any] = Body(default_factory=dict)):
    missing = _unknown_skill(body.get("action"))
    if missing is not None:
        return JSONResponse({"ok": False, "error": f"Unknown skill: {missing}"}, status_code=400)
    try:
        task = task_store.create_task(
            title=body.get("title"),
            recurrence=body.get("recurrence") or {},
            action=body.get("action") or {},
            enabled=bool(body.get("enabled", True)),
            notify=str(body.get("notify") or "on_error"))
    except (ValueError, KeyError) as err:
        return JSONResponse({"ok": False, "error": str(err) or "Could not create that task."},
                            status_code=400)
    return {"ok": True, "task": task}


@router.patch("/tasks/{task_id}")
def update(task_id: str, body: dict[str, Any] = Body(default_factory=dict)):
    missing = _unknown_skill(body.get("action"))
    if missing is not None:
        return JSONResponse({"ok": False, "error": f"Unknown skill: {missing}"}, status_code=400)
    try:
        task = task_store.update_task(task_id, body)
    except KeyError:
        return JSONResponse({"ok": False, "error": "Unknown task."}, status_code=404)
    except ValueError as err:
        return JSONResponse({"ok": False, "error": str(err) or "Could not update that task."},
                            status_code=400)
    return {"ok": True, "task": task}


@router.delete("/tasks/{task_id}")
def remove(task_id: str) -> dict[str, Any]:
    # Deleting something already gone is not an error: the caller wanted it
    # gone, and it is. Matches the recorded behaviour.
    task_store.delete_task(task_id)
    return {"ok": True}


@router.post("/tasks/{task_id}/run")
def run_now(task_id: str):
    """Run it this instant, without waiting for its own schedule.

    The task's own confirmation rules are untouched by this: a HIGH-risk action
    inside it still asks, exactly as it would on a scheduled run — running early
    is not consent to anything the task does.
    """
    from ..scheduler.engine import run_task_now

    try:
        result = run_task_now(task_id)
    except KeyError:
        return JSONResponse({"ok": False, "error": "Unknown task."}, status_code=404)
    except Exception as err:  # noqa: BLE001 — the caller gets the reason, not a 500
        return JSONResponse({"ok": False, "error": str(err) or "Could not run that task."},
                            status_code=400)
    return {"ok": True, "result": result}


@router.get("/task-runs")
def runs(taskId: str | None = None) -> dict[str, Any]:  # noqa: N803 — the recorded query name
    return {"runs": task_store.list_runs(taskId)}
