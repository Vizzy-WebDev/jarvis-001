"""Running the registered checks, and the one automatic attempt at a fix.

**One remedy per NEW failure, never per tick.** Only an ok-to-failing transition
spends a remedy and produces a finding; a check still failing on the next tick
does neither. Without that, one broken thing means a remedy attempt, a model
call to judge urgency and a notification every few minutes for as long as it
stays broken — which is both expensive and the fastest way to train someone to
ignore notifications.

The state that makes this possible is the schedule row's own `checkState`, the
same mechanism every other source uses for the same reason.

Every probe, remedy and outcome writes a trace row, which is what makes "has
anything gone wrong with you lately?" a real answer rather than a claim.
"""

from __future__ import annotations

import logging
from typing import Any

from ...heartbeat.sources.registry import Source
from .. import trace as ops_trace
from .registry import get_check, list_checks

logger = logging.getLogger(__name__)

SOURCE_ID = "diagnosis"
#: Cheap probes only — real reads, no model call of their own.
CHECK_INTERVAL_MS = 5 * 60 * 1000


def _trace(check_id: str, phase: str, kind: str, summary: str, detail: Any = None) -> None:
    try:
        ops_trace.append(source=SOURCE_ID, source_ref=check_id, phase=phase, effect="read",
                         kind=kind, summary=summary, detail=detail)
    except Exception:  # noqa: BLE001 — a trace failure must not fail the diagnosis
        logger.exception("could not trace diagnosis of %s", check_id)


def list_items() -> list[dict[str, Any]]:
    return [{"itemKey": check.id, "intervalMs": CHECK_INTERVAL_MS} for check in list_checks()]


def _probe(check: Any) -> dict[str, Any]:
    try:
        return check.probe() or {"ok": False, "detail": "The check returned nothing."}
    except Exception as err:  # noqa: BLE001 — a check that throws IS a failing check
        return {"ok": False, "detail": f"The check itself failed: {err}"}


def check(item_key: str) -> dict[str, Any]:
    from ...heartbeat import schedule_store

    registered = get_check(item_key)
    if registered is None:
        # Unregistered since this item was scheduled: nothing to run, and the
        # row is pruned on the next reconcile.
        return {"finding": None, "checkState": None}

    item = schedule_store.get_item(SOURCE_ID, item_key)
    was_failing = ((item or {}).get("checkState") or {}).get("lastOutcome") == "failing"

    _trace(item_key, "intent", "probe", f"Checking: {item_key}")
    result = _probe(registered)
    _trace(item_key, "outcome", "probe",
           "Passed." if result.get("ok") else f"Failed: {result.get('detail') or 'no detail'}",
           result.get("detail"))

    if result.get("ok"):
        if was_failing:
            _trace(item_key, "outcome", "note", f'"{item_key}" is working again.')
        return {"finding": None, "checkState": {"lastOutcome": "ok"}}

    if was_failing:
        # Already had its one attempt and was already reported. Nothing new has
        # happened, so nothing new is said.
        return {"finding": None}

    if registered.remedy is not None:
        _trace(item_key, "intent", "remedy", f'Trying the one automatic fix for "{item_key}".')
        try:
            registered.remedy()
        except Exception as err:  # noqa: BLE001
            _trace(item_key, "outcome", "remedy", f"The fix itself failed: {err}")
        again = _probe(registered)
        if again.get("ok"):
            _trace(item_key, "outcome", "remedy", f'"{item_key}" fixed itself after one attempt.')
            return {"finding": None, "checkState": {"lastOutcome": "ok"}}
        _trace(item_key, "outcome", "decision",
               f'"{item_key}" is still failing after the one automatic fix.', again.get("detail"))
        return {
            "finding": {"summary": f'Something is wrong with me: "{item_key}" is failing, and '
                                   f"trying to fix it automatically did not work.",
                        "detail": again.get("detail") or result.get("detail")},
            "checkState": {"lastOutcome": "failing"},
        }

    _trace(item_key, "outcome", "decision",
           f'"{item_key}" is failing and has no automatic fix.', result.get("detail"))
    return {"finding": {"summary": f'Something is wrong with me: "{item_key}" is failing.',
                        "detail": result.get("detail")},
            "checkState": {"lastOutcome": "failing"}}


source = Source(id=SOURCE_ID, default_interval_ms=CHECK_INTERVAL_MS,
                list_items=list_items, check=check)


def recent_health(hours: int = 24, limit: int = 200) -> dict[str, Any]:
    """What the checks have actually found lately — the pull path's raw material."""
    from datetime import datetime, timedelta, timezone

    from ...jscompat import to_iso_z

    since = to_iso_z(datetime.now(timezone.utc) - timedelta(hours=hours))
    rows = ops_trace.recent(SOURCE_ID, since_iso=since, limit=limit)
    failing: dict[str, str] = {}
    from ...heartbeat import schedule_store
    for item in schedule_store.list_for_source(SOURCE_ID):
        if ((item.get("checkState") or {}).get("lastOutcome")) == "failing":
            failing[item["itemKey"]] = "failing"
    return {
        "hours": hours,
        "checksRegistered": [c.id for c in list_checks()],
        "currentlyFailing": sorted(failing),
        "recent": [{"check": r["source_ref"], "phase": r["phase"], "kind": r["kind"],
                    "summary": r["summary"], "at": r["created_at"]} for r in rows],
    }
