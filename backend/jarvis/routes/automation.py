"""Two small surfaces that belong to the same part of the interface: the morning
briefing's settings, and the things Jarvis is currently watching for.

**There is deliberately no route that STARTS a monitor.** Starting one is always
the watching capability's job, which works out a concrete check from what the
person actually said before anything is created. A "create a monitor" endpoint
would be a way to start one blind, with a check nobody had reasoned about. This
reads the list and stops one, and that is the whole surface.

Stopping broadcasts, so every open tab clears its watching indicator rather than
only the one whose button was clicked — the same event the spoken "stop watching
that" sends, so a click and a sentence can never leave the interface disagreeing.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from ..events import EventType, bus
from ..monitor import store as monitor_store
from ..scheduler import briefing, briefing_config

router = APIRouter(prefix="/api")


# --- the morning briefing ------------------------------------------------------

@router.get("/briefing")
def config() -> dict[str, Any]:
    return briefing_config.get_config()


@router.post("/briefing")
def save(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    """Merged, never replaced — the store owns which keys exist, so a screen that
    knows about five sections cannot silently drop a sixth it has not heard of."""
    return briefing_config.set_config(body)


@router.post("/briefing/preview")
def preview() -> dict[str, Any]:
    """Compose one now, for real, with a real model call.

    Reading the settings and imagining the result is not the same as hearing it,
    and this is the only way to find out that a section is worded badly before
    it is the first thing said in the morning.
    """
    result = briefing.compose_briefing()
    if not result.ok:
        return {"ok": False, "text": "Could not put the briefing together right now.",
                "error": result.error, "facts": result.facts}
    return {"ok": True, "text": result.text, "facts": result.facts,
            "modelId": result.model_id}


# --- what is being watched for --------------------------------------------------

@router.get("/monitors")
def monitors() -> dict[str, Any]:
    return {"monitors": monitor_store.list_monitors()}


@router.post("/monitors/{monitor_id}/stop")
def stop(monitor_id: str):
    monitor = monitor_store.get_monitor(monitor_id)
    if monitor is None:
        return JSONResponse({"ok": False, "error": "Unknown monitor."}, status_code=404)
    monitor_store.stop_monitor(monitor_id)
    bus.publish(EventType.MONITOR_STOPPED,
                {"monitorId": monitor_id, "description": monitor.get("description")})
    return {"ok": True}
