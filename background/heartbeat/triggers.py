"""Reacting to something happening, rather than waiting for the next poll.

Feeds the SAME `route_finding()` the tick does — one pipeline, two entry points.
A second pipeline would mean two places deciding whether to interrupt someone.

Subscribes to the event bus rather than being called by the jobs code: a job
already publishes when it parks, and having the jobs subsystem know that a
heartbeat exists would be the coupling the bus was built to remove.

Only `awaiting_decision` is reacted to. Completions and failures already have
their own notification the moment they happen; this is for the one transition
that otherwise sits silent.
"""

from __future__ import annotations

import logging
from typing import Callable

from ..events import EventType, bus as default_bus
from ..events.bus import Event, EventBus

logger = logging.getLogger(__name__)

_unsubscribes: list[Callable[[], None]] = []


def _on_job_updated(event: Event) -> None:
    if event.payload.get("status") != "awaiting_decision":
        return
    job_id = event.payload.get("id")
    if not job_id:
        return

    from . import schedule_store
    from .engine import route_finding
    from .sources.jobs_source import source as jobs_source

    try:
        # Ensure a schedule row exists and persist whatever memory this check
        # produces. Without that, the trigger path would never write the dedup
        # state, and the next ordinary tick would report the same ask again.
        schedule_store.upsert_item(jobs_source.id, job_id, jobs_source.default_interval_ms)
        outcome = jobs_source.check(job_id) or {}
        if "checkState" in outcome:
            schedule_store.mark_done(f"{jobs_source.id}:{job_id}", outcome["checkState"])
        else:
            schedule_store.mark_done(f"{jobs_source.id}:{job_id}")
        if outcome.get("finding"):
            route_finding(jobs_source.id, job_id, outcome["finding"])
    except Exception:  # noqa: BLE001 — a trigger must never break the thing that fired it
        logger.exception("the trigger check for job %s failed", job_id)


def start_triggers(event_bus: EventBus | None = None) -> None:
    """Idempotent: a second call replaces the first rather than doubling it."""
    stop_triggers()
    ebus = event_bus or default_bus
    _unsubscribes.append(ebus.subscribe(EventType.JOB_UPDATED, _on_job_updated))


def stop_triggers() -> None:
    while _unsubscribes:
        try:
            _unsubscribes.pop()()
        except Exception:  # noqa: BLE001
            logger.warning("could not detach a heartbeat trigger")
