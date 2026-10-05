"""Active and recent watches (`data/monitors.json`).

A leaf: the engine and the tool both depend on this, and it depends on neither.
A monitor is one "watch for X, then do Y" — a one-shot watch rather than a
recurring job, which is why it keeps no run history the way a scheduled task
does.
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

from ..jscompat import now_iso
from ..store import read_json, write_json

FILE = "monitors"
MAX_KEPT = 100

_lock = threading.RLock()


def _load() -> dict[str, Any]:
    data = read_json(FILE, {"monitors": []})
    return data if isinstance(data, dict) and isinstance(data.get("monitors"), list) \
        else {"monitors": []}


def list_monitors() -> list[dict[str, Any]]:
    return _load()["monitors"]


def list_watching() -> list[dict[str, Any]]:
    return [m for m in list_monitors() if m.get("status") == "watching"]


def get_monitor(monitor_id: str) -> dict[str, Any] | None:
    return next((m for m in list_monitors() if m.get("id") == monitor_id), None)


def create_monitor(*, description: str, check: dict[str, Any], on_trigger: dict[str, Any],
                   ) -> dict[str, Any]:
    with _lock:
        data = _load()
        monitor = {
            "id": f"mon_{uuid.uuid4().hex[:10]}",
            "description": description,
            "check": check,
            "onTrigger": on_trigger,
            "status": "watching",
            "createdAt": now_iso(),
            "lastCheckedAt": None,
            "triggeredAt": None,
            "state": None,
            "error": None,
        }
        data["monitors"].insert(0, monitor)
        data["monitors"] = data["monitors"][:MAX_KEPT]
        write_json(FILE, data)
        return monitor


def update_monitor(monitor_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    with _lock:
        data = _load()
        for index, monitor in enumerate(data["monitors"]):
            if monitor.get("id") == monitor_id:
                data["monitors"][index] = {**monitor, **patch}
                write_json(FILE, data)
                return data["monitors"][index]
        return None


def stop_monitor(monitor_id: str, reason: str = "stopped") -> dict[str, Any] | None:
    return update_monitor(monitor_id, {"status": "stopped", "error": None,
                                       "stoppedReason": reason})
