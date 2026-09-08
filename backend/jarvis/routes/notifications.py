"""The bell: what Jarvis has told the user, and what they have not seen yet.

Reading is a GET, marking read is a POST, and clearing is a DELETE — the shapes
the recorded API already answers with, so an existing caller needs no changes.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from .. import notifications

router = APIRouter(prefix="/api")


@router.get("/notifications")
def listed(limit: int | None = None) -> dict[str, Any]:
    return {"notifications": notifications.listed(limit)}


@router.post("/notifications")
def create(body: dict[str, Any] = Body(default_factory=dict)):
    """Adding one by hand. The front end does not normally need this — every
    real notice comes from a subsystem publishing on the bus — but the original
    exposes it and a UI action may legitimately want to record something."""
    try:
        return notifications.add(
            kind=str(body.get("kind") or "system"),
            level=str(body.get("level") or "info"),
            title=str(body.get("title") or ""),
            body=str(body.get("body") or ""),
            action=body.get("action") if isinstance(body.get("action"), dict) else None,
            meta=body.get("meta") if isinstance(body.get("meta"), dict) else None)
    except ValueError as err:
        return JSONResponse({"error": str(err)}, status_code=400)


@router.post("/notifications/read")
def read(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    ids = body.get("ids") if isinstance(body.get("ids"), list) else [body.get("id")]
    return {"notifications": notifications.mark_read([i for i in ids if i])}


@router.post("/notifications/read-all")
def read_all() -> dict[str, Any]:
    return {"notifications": notifications.mark_all_read()}


@router.delete("/notifications/{notification_id}")
def remove(notification_id: str):
    if not notifications.remove(notification_id):
        return JSONResponse({"ok": False, "error": "Not found."}, status_code=404)
    return {"ok": True}


@router.delete("/notifications")
def clear() -> dict[str, Any]:
    notifications.clear_all()
    return {"ok": True}
