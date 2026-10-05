"""Watching and stopping a control session, and the screen-watching badge.

Reading status is a GET; stopping is a POST and needs no body — the three stops
are meant to be reachable in one action, from a button, a hotkey handler or a
panicked click, not composed into a request.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

from ..control import captures as capture_store
from ..control import session as control
from ..control import watching

router = APIRouter(prefix="/api")

_NOT_FOUND = {"ok": False, "error": "Not found."}


@router.get("/control/status")
def control_status() -> dict:
    """The four fields the recorded API already answers with, plus this build's
    own detail only when there is a session to describe. An idle answer stays
    byte-identical to the one every existing caller was written against."""
    session = control.active()
    base = {"active": False, "step": "", "awaitingConfirmation": False,
            "pendingSummary": None}
    if session is None:
        return base
    detail = session.as_dict()
    return {**base, **detail,
            "active": detail["active"],
            "step": session.summary or f"step {session.step}",
            "awaitingConfirmation": session.status == control.AWAITING_CONFIRMATION,
            "pendingSummary": session.summary or None}


@router.post("/control/stop")
def stop(body: dict | None = None) -> dict:
    reason = str((body or {}).get("reason") or "").strip() or "you asked me to stop"
    return control.request_stop(reason)


@router.get("/observation/status")
def observation_status() -> dict:
    """`active` is "is the badge lit", which is a glance OR sharing — the
    recorded shape, with the reasons added."""
    state = watching.state()
    return {"active": state.watching, "sharing": state.sharing,
            "reasons": list(state.reasons)}


@router.post("/observation/share/start")
def start_sharing() -> dict:
    return {"ok": True, **watching.start_sharing().as_dict()}


@router.post("/observation/share/stop")
def stop_sharing() -> dict:
    return {"ok": True, **watching.stop_sharing().as_dict()}


@router.post("/observation/stop")
def stop_watching() -> dict:
    """One "stop whatever is making this badge lit" action — what clicking the
    badge does, and it ends sharing as well as any glance in progress."""
    return {"ok": True, **watching.stop_everything().as_dict()}


@router.get("/control/safety")
def read_safety() -> dict:
    from ..control.safety import get_safety_config

    return get_safety_config()


@router.post("/control/safety")
def write_safety(body: dict | None = None):
    from ..control.safety import set_safety_config

    if not isinstance(body, dict) or not body:
        return JSONResponse({"error": "Nothing to change."}, status_code=400)
    return set_safety_config(body)


# --- what was captured -------------------------------------------------------
#
# Screenshots and recordings render INLINE, where an artifact is always a forced
# download. That difference is deliberate: an artifact's content is whatever a
# model wrote (an `.svg` it authored can carry script), while these bytes came
# from the operating system's own screen capture, and showing a screenshot is
# the entire point of taking one. The parts a model could influence are removed
# rather than trusted — the id is generated here and matched against a fixed
# shape, no filename comes from a tool argument, and the type is fixed per kind
# rather than sniffed. `nosniff` and a sandboxing CSP stay as defence in depth.

def _serve(kind_name: str, capture_id: str):
    capture = capture_store.get(kind_name, capture_id)
    if capture is None:
        return JSONResponse(_NOT_FOUND, status_code=404)
    kind = capture_store.KINDS[kind_name]
    return FileResponse(
        capture.path,
        media_type=kind.mime_type,
        headers={
            "Content-Disposition": f'inline; filename="{capture.id}{kind.suffix}"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            # A capture is a moment, not a document: a stale one misleads.
            "Cache-Control": "no-store",
        },
    )


@router.get("/control/screenshots")
def list_screenshots() -> dict:
    return {"screenshots": [c.as_dict() for c in capture_store.recent(capture_store.SCREENSHOT)]}


@router.get("/control/screenshots/{capture_id}")
def serve_screenshot(capture_id: str):
    return _serve(capture_store.SCREENSHOT.name, capture_id)


@router.get("/control/recordings")
def list_recordings() -> dict:
    return {"recordings": [c.as_dict() for c in capture_store.recent(capture_store.RECORDING)]}


@router.get("/control/recordings/{capture_id}")
def serve_recording(capture_id: str):
    return _serve(capture_store.RECORDING.name, capture_id)
