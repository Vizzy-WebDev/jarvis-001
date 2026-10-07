"""How the computer and Jarvis are doing, for Home's two health cards.

Read-only, and a read of what other parts already know — the environment sampler's
psutil reading, the connector store's own status, the job store, the model traces —
never a new probe. Probing here would be a second, differently-timed opinion about
the same fact (`ops/environment/reachability.py` says the same for the same reason).
Polled by an open Home every few seconds, so every part is cheap: one psutil call
each, and indexed reads of today's traces.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any

import psutil
from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

#: The last network counter reading, so throughput is measured between polls
#: rather than since boot (which would read as the same number forever).
_net_lock = threading.Lock()
_net_last: tuple[float, int] | None = None


def _network_bytes_per_second() -> float | None:
    """Bytes moved per second since the previous call. None on the first call —
    a rate needs an interval, and pretending the first one is zero would show an
    idle line that never was."""
    global _net_last
    try:
        counters = psutil.net_io_counters()
    except Exception:  # noqa: BLE001 - not every machine exposes counters
        return None
    now = time.monotonic()
    total = int(counters.bytes_sent + counters.bytes_recv)
    with _net_lock:
        previous, _net_last = _net_last, (now, total)
    if previous is None or now <= previous[0]:
        return None
    return max(0.0, (total - previous[1]) / (now - previous[0]))


def system() -> dict[str, Any]:
    from ..ops.environment import sampler
    from ..store import data_dir

    reading = sampler.read_now()
    mem_free = reading.get("memFreePct")
    try:
        disk = float(psutil.disk_usage(str(data_dir())).percent)
    except Exception:  # noqa: BLE001
        disk = None
    return {
        "cpuPct": None if reading.get("cpuPct") is None else round(reading["cpuPct"]),
        "memPct": None if mem_free is None else round(100 - mem_free),
        "diskPct": None if disk is None else round(disk),
        "netBytesPerSec": _network_bytes_per_second(),
    }


def _connectors() -> dict[str, Any]:
    from ..connectors import store

    enabled = [c for c in store.list_connectors() if c.get("enabled")]
    problems = [c for c in enabled if (c.get("status") or {}).get("state") == "error"]
    return {"enabled": len(enabled),
            "working": len(enabled) - len(problems),
            "problems": [c.get("label") or c.get("name") or c.get("id") for c in problems]}


def _today_start() -> str:
    """Local midnight as the traces' own ISO form (UTC), so 'today' is the
    person's day, not the server clock's."""
    midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    from datetime import timezone
    return midnight.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _models_today() -> dict[str, Any]:
    """How quickly replies start (the median time to first token over today's
    last answered calls) and what today has cost. A day with no calls says
    so with None rather than a zero that reads as "instant" or "free"."""
    from ..db import get_db
    from ..models import trace

    since = _today_start()
    with trace._lock:  # noqa: SLF001 - the trace store's own lock over the shared connection
        rows = get_db().execute(
            "SELECT ttft_ms FROM model_traces WHERE outcome = 'ok' AND at >= ? "
            "AND ttft_ms IS NOT NULL ORDER BY at DESC LIMIT 25", (since,)).fetchall()
    firsts = sorted(int(r[0]) for r in rows)
    response = round(firsts[len(firsts) // 2] / 1000, 1) if firsts else None
    spent = trace.spend("day", since=since)
    cost = sum(float(r["cost"]) for r in spent) if spent else 0.0
    priced = sum(int(r["calls"]) - int(r["unpriced_calls"]) for r in spent)
    unpriced = sum(int(r["unpriced_calls"]) for r in spent)
    return {"responseSec": response,
            "spendToday": round(cost, 2) if priced else None,
            "unpricedCalls": unpriced}


def jarvis() -> dict[str, Any]:
    from ..assembly import get_wake_detector
    from ..jobs import job_store
    from ..models import settings

    issues: list[str] = []
    models_ok = settings.availability().state == "ok"
    if not models_ok:
        issues.append("No model is ready to answer")
    connectors = _connectors()
    issues += [f"{name} needs attention" for name in connectors["problems"]]
    wake = get_wake_detector().status()
    try:
        models = _models_today()
    except Exception:  # noqa: BLE001 - a health card never fails the page
        logger.exception("health: could not read today's model traces")
        models = {"responseSec": None, "spendToday": None, "unpricedCalls": 0}
    return {
        "issues": issues,
        "modelReady": models_ok,
        "uptimeSec": round(time.time() - psutil.Process().create_time()),
        "voice": {"wakeWord": bool(wake.get("available")),
                  "wakeWordNote": wake.get("note")},
        "connectors": connectors,
        "jobsRunning": len(job_store.list_active_jobs()),
        **models,
    }


@router.get("/health")
def health() -> dict[str, Any]:
    return {"system": system(), "jarvis": jarvis()}
