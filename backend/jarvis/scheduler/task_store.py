"""Scheduled tasks and their run history.

A leaf: plain CRUD over `data/tasks.json` and `data/task-runs.json`, split from
the engine so the briefing composer and the tick loop can both read tasks without
importing each other.

Byte-compatible with the Node implementation — the same keys in the same order,
so both can read the same file during the port.
"""

from __future__ import annotations

import random
import threading
from datetime import datetime
from typing import Any

from ..jscompat import now_iso, to_iso_z
from ..store import read_json, write_json
from .recurrence import describe, next_run_at

TASKS_FILE = "tasks"
RUNS_FILE = "task-runs"
MAX_RUNS_KEPT = 200

_lock = threading.RLock()


def _iso(moment: datetime | None) -> str | None:
    """UTC with a trailing Z, exactly as `Date.toISOString()` writes it — the
    same file is read by both implementations during the port, and a local
    naive timestamp would compare and sort differently against every row the
    Node app wrote."""
    return to_iso_z(moment) if moment else None


def _make_id(prefix: str, extra: int = 4) -> str:
    stamp = _base36(int(datetime.now().timestamp() * 1000))
    tail = "".join(random.choice("0123456789abcdefghijklmnopqrstuvwxyz") for _ in range(extra))
    return f"{prefix}{stamp}{tail}"


def _base36(value: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return "0"
    out = ""
    while value:
        value, remainder = divmod(value, 36)
        out = digits[remainder] + out
    return out


def _load_tasks() -> dict[str, Any]:
    data = read_json(TASKS_FILE, {"tasks": []})
    return data if isinstance(data, dict) and isinstance(data.get("tasks"), list) else {"tasks": []}


def _load_runs() -> dict[str, Any]:
    data = read_json(RUNS_FILE, {"runs": []})
    return data if isinstance(data, dict) and isinstance(data.get("runs"), list) else {"runs": []}


def list_tasks() -> list[dict[str, Any]]:
    return _load_tasks()["tasks"]


def get_task(task_id: str) -> dict[str, Any] | None:
    return next((t for t in list_tasks() if t.get("id") == task_id), None)


def create_task(*, title: str | None = None, recurrence: dict[str, Any],
                action: dict[str, Any], enabled: bool = True,
                notify: str = "on_error") -> dict[str, Any]:
    if not (recurrence or {}).get("type"):
        raise ValueError("A task needs a recurrence.")
    if not (action or {}).get("type"):
        raise ValueError("A task needs an action.")
    with _lock:
        data = _load_tasks()
        task = {
            "id": _make_id("t"),
            "title": title or describe(recurrence),
            "recurrence": recurrence,
            "action": action,
            "enabled": enabled,
            # 'always' | 'on_error' | 'never'. Defaults to on_error so a task
            # created by voice does not announce every routine run.
            "notify": notify,
            "nextRunAt": _iso(next_run_at(recurrence, datetime.now())) if enabled else None,
            "createdAt": now_iso(),
            "lastRunAt": None,
        }
        data["tasks"].append(task)
        write_json(TASKS_FILE, data)
        return task


def update_task(task_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        data = _load_tasks()
        index = next((i for i, t in enumerate(data["tasks"]) if t.get("id") == task_id), -1)
        if index == -1:
            raise KeyError(f"Unknown task: {task_id}")
        task = {**data["tasks"][index], **patch}
        if "recurrence" in patch or "enabled" in patch:
            task["nextRunAt"] = (_iso(next_run_at(task["recurrence"], datetime.now()))
                                 if task.get("enabled") else None)
        data["tasks"][index] = task
        write_json(TASKS_FILE, data)
        return task


def delete_task(task_id: str) -> None:
    with _lock:
        data = _load_tasks()
        data["tasks"] = [t for t in data["tasks"] if t.get("id") != task_id]
        write_json(TASKS_FILE, data)


def list_runs(task_id: str | None = None) -> list[dict[str, Any]]:
    runs = _load_runs()["runs"]
    return [r for r in runs if r.get("taskId") == task_id] if task_id else runs


def record_run(run: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        data = _load_runs()
        full = {"id": _make_id("r", 6), **run}
        data["runs"].insert(0, full)
        # Capped: run history is a log, not an archive, and an unbounded JSON
        # file is rewritten in full on every single run.
        data["runs"] = data["runs"][:MAX_RUNS_KEPT]
        write_json(RUNS_FILE, data)
        return full
