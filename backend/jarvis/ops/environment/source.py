"""System load, as something worth noticing.

One item: load is not per-entity the way a job or a commitment is.

Its own `checkState` dedup, for the reason the source contract spells out — a
sustained-high-load condition holds true for as long as it lasts, and a finding
per tick would mean a model call and a notification every few minutes for one
situation the user already knows about. A finding is produced only when the KIND
of anomaly changes (nothing to CPU, CPU to memory, back to nothing).
"""

from __future__ import annotations

import json
from typing import Any

from ...heartbeat.sources.registry import Source
from . import baseline

SOURCE_ID = "environment"
CHECK_INTERVAL_MS = 5 * 60 * 1000


def list_items() -> list[dict[str, Any]]:
    return [{"itemKey": "system-load", "intervalMs": CHECK_INTERVAL_MS}]


def check(item_key: str) -> dict[str, Any]:
    from ...heartbeat import schedule_store

    anomaly = baseline.check_for_anomaly()
    kind = json.loads(anomaly["detail"])["kind"] if anomaly else None

    item = schedule_store.get_item(SOURCE_ID, item_key)
    reported = ((item or {}).get("checkState") or {}).get("reportedKind")

    if anomaly is None:
        # Settled: forget the past report, so a genuine recurrence is reported
        # again rather than suppressed forever by a stale memory.
        return {"finding": None, "checkState": None} if reported else {"finding": None}

    if reported == kind:
        return {"finding": None}         # the same situation, still true

    return {"finding": {"summary": anomaly["summary"], "detail": anomaly["detail"]},
            "checkState": {"reportedKind": kind}}


source = Source(id=SOURCE_ID, default_interval_ms=CHECK_INTERVAL_MS,
                list_items=list_items, check=check)
