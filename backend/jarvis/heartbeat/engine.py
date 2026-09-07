"""The tick, and the one path every finding takes to reach a person.

Two entry points — this tick and the event-driven trigger — and exactly one
pipeline, `route_finding()`. Two pipelines would mean two places where "should
this interrupt someone" is decided, and they would diverge.

**Sequential and capped.** A restart, or a machine that was asleep, leaves many
items overdue at once. Processing them one at a time up to a per-tick limit turns
that into several ticks of steady work instead of one burst that could spend
twenty model calls in a second.

**One broken source never stops another.** A source whose `list_items()` throws
is skipped for the tick; an item whose `check()` throws still advances its own
schedule, in a `finally`, so a consistently failing check does not retry on every
tick forever.

Off behind an interlock until cutover: the Node app is still the live one, and
two builds noticing the same thing would say it twice.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

from ..events import EventType, bus as default_bus
from ..events.bus import EventBus
from ..jscompat import now_iso
from . import outbox, schedule_store
from .decision import decide_attention
from .sources.registry import get_source, list_sources

logger = logging.getLogger(__name__)

TICK_SECONDS = 60.0
PER_TICK_CAP = 20
ENABLE_ENV = "JARVIS_HEARTBEAT"

_timer: threading.Timer | None = None


def is_enabled() -> bool:
    return os.environ.get(ENABLE_ENV) == "1"


# --- the one pipeline ---------------------------------------------------------

def route_finding(source_id: str, item_key: str, finding: dict[str, Any], *,
                  event_bus: EventBus | None = None) -> dict[str, Any]:
    """Judge one finding and do whatever that judgment calls for."""
    ebus = event_bus or default_bus
    source_ref = f"{source_id}:{item_key}"

    if outbox.pending_for_source_ref("heartbeat", source_ref):
        # Already waiting and not yet dealt with. Saying it twice does not make
        # it more likely to be acted on.
        return {"routed": False, "why": "already waiting"}

    try:
        verdict = decide_attention(finding)
    except Exception:  # noqa: BLE001 — the judgment failing must not lose the finding
        logger.exception("the urgency check itself failed")
        from .decision import Verdict
        verdict = Verdict(tier=3, reason="The urgency check failed, so this was only recorded.")

    # The durable record, written before any delivery is considered — which is
    # what makes "a notice is never lost because nobody was there" structural
    # rather than usually true.
    ebus.publish(EventType.NOTIFICATION_CREATED, {
        "kind": "heartbeat", "level": "warning" if verdict.tier == 1 else "info",
        "title": finding.get("summary"), "body": verdict.reason,
        "meta": {"sourceId": source_id, "itemKey": item_key, "tier": verdict.tier},
        "at": now_iso()})

    if verdict.tier == 3:
        return {"routed": True, "tier": 3, "spoken": False}

    entry_id = outbox.add(source="heartbeat", source_ref=source_ref, tier=verdict.tier,
                          reason="notice", summary=str(finding.get("summary") or ""),
                          detail=finding.get("detail"))

    if verdict.tier != 1:
        # Tier 2 drains into the next turn the user starts, like any other row.
        return {"routed": True, "tier": 2, "outboxId": entry_id, "spoken": False}

    if _may_speak_now(verdict):
        from .speak import speak_now
        speak_now(str(finding.get("summary") or ""), outbox_id=entry_id,
                  reason=verdict.reason, event_bus=ebus)
        return {"routed": True, "tier": 1, "outboxId": entry_id, "spoken": True}

    return {"routed": True, "tier": 1, "outboxId": entry_id, "spoken": False}


def _may_speak_now(verdict: Any) -> bool:
    """Quiet hours and the busy dampener, in the order that lets an emergency
    skip exactly the two it should and neither of the ones it should not."""
    from . import presence
    from .quiet_hours import is_quiet_now

    if is_quiet_now():
        # Reachability is never skipped: with nobody there, there is no delivery
        # to make, emergency or not.
        return bool(verdict.emergency) and presence.is_reachable()
    return presence.is_available()


# --- the tick -----------------------------------------------------------------

def reconcile(source: Any) -> None:
    items = source.list_items()
    for item in items:
        schedule_store.upsert_item(source.id, item["itemKey"],
                                   item.get("intervalMs") or source.default_interval_ms)
    schedule_store.prune_removed(source.id, [item["itemKey"] for item in items])


def process_item(item: dict[str, Any], *, event_bus: EventBus | None = None) -> dict[str, Any]:
    source = get_source(item["sourceId"])
    schedule_store.mark_running(item["id"])
    result: dict[str, Any] = {"item": item["id"], "checked": False}
    check_state: Any = None
    has_state = False
    try:
        if source is None:
            # The source that created this row is gone; the next reconcile prunes
            # it. Nothing to check, and nothing to report about it.
            return result
        outcome = source.check(item["itemKey"]) or {}
        result["checked"] = True
        if "checkState" in outcome:
            check_state, has_state = outcome["checkState"], True
        if outcome.get("finding"):
            result["routed"] = route_finding(source.id, item["itemKey"], outcome["finding"],
                                             event_bus=event_bus)
    except Exception:  # noqa: BLE001 — one item's failure is not the tick's
        logger.exception('source "%s" failed checking "%s"', item["sourceId"], item["itemKey"])
        result["error"] = True
    finally:
        # In a `finally` on purpose: a check that throws still advances, or it
        # is retried on every tick forever.
        if has_state:
            schedule_store.mark_done(item["id"], check_state)
        else:
            schedule_store.mark_done(item["id"])
    return result


def tick(*, event_bus: EventBus | None = None, now: str | None = None) -> list[dict[str, Any]]:
    for source in list_sources():
        try:
            reconcile(source)
        except Exception:  # noqa: BLE001
            logger.exception('source "%s" could not list its items; skipped this tick', source.id)

    return [process_item(item, event_bus=event_bus)
            for item in schedule_store.list_due(now, PER_TICK_CAP)]


def start(*, event_bus: EventBus | None = None) -> bool:
    global _timer
    if not is_enabled() or _timer is not None:
        return False

    # Startup only: a row left `running` by a crash mid-check would otherwise
    # wedge that item forever.
    schedule_store.reset_stale_running()
    register_default_sources()

    def run() -> None:
        global _timer
        try:
            tick(event_bus=event_bus)
        except Exception:  # noqa: BLE001
            logger.exception("a heartbeat tick failed")
        _timer = threading.Timer(TICK_SECONDS, run)
        _timer.daemon = True
        _timer.start()

    run()
    return True


def stop() -> None:
    global _timer
    if _timer is not None:
        _timer.cancel()
        _timer = None


def register_default_sources() -> None:
    """Everything watched by default, in one place."""
    from ..ops.diagnostics import checks as diagnostic_checks
    from ..ops.diagnostics.source import source as diagnosis_source
    from ..ops.environment.source import source as environment_source
    from .sources.commitments_source import source as commitments_source
    from .sources.jobs_source import source as jobs_source
    from .sources.registry import register

    register(jobs_source)
    register(commitments_source)
    register(environment_source)
    # Registered before the diagnosis source, so its very first tick has real
    # checks to run rather than an empty list.
    diagnostic_checks.register_all()
    register(diagnosis_source)
