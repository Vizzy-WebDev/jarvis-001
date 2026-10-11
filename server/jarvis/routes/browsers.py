"""Jarvis's own browser over HTTP: sessions, a live picture, and the "Take control" page.

Ported from OpenMuse's `/api/browsers/*` routes (MIT licence, CopilotKit) onto
Jarvis's conventions: answers are `{"ok": ..., "error": ...}` like the rest of the
API, and there are no signed URLs - Jarvis binds to this computer only, and
`request_guard.py` already refuses anything a browser marks as coming from another
site (or from a sandboxed page Jarvis made), which is what the signing was for.

The picture of a page is the one thing here that comes from the outside world.
It is only ever pixels Chromium drew, but it is still served the way every
other piece of outside content is: `Content-Disposition: attachment`
unconditionally, `nosniff` and a sandboxing CSP. The console that shows it
FETCHES the bytes and draws them itself, and an `<img>` is unaffected by the
download header, so nothing needs the inline form.

Handlers are plain `def`: they block on the browser's own thread, so FastAPI runs
them in its thread pool rather than on the event loop.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Body, Response
from fastapi.responses import HTMLResponse, JSONResponse

from ..browser import BrowserError, get_host
from ..browser.console import CONSOLE_CSP, console_page
from ..browser.manager import validate_session_id

router = APIRouter(prefix="/api/browsers")

_OUTSIDE_CONTENT = {
    "Content-Disposition": 'attachment; filename="browser.png"',
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; sandbox",
    "Cache-Control": "no-store",
}


def _fail(err: BrowserError) -> JSONResponse:
    return JSONResponse({"ok": False, "error": err.message, "code": err.code},
                        status_code=err.status)


def _url_of(body: dict[str, Any]) -> str:
    url = body.get("url")
    if not isinstance(url, str) or not url.strip() or len(url) > 8192:
        raise BrowserError("INVALID_URL", "A web address of at most 8192 characters is needed.")
    return url.strip()


@router.get("/status")
def status() -> dict:
    """Whether a browser can be opened on this computer - answered without opening one."""
    host = get_host()
    return {"ok": True, **host.availability(), "started": host.started}


@router.get("")
def list_sessions():
    try:
        return {"ok": True, "sessions": get_host().sessions()}
    except BrowserError as err:
        return _fail(err)


@router.post("")
def open_new(body: dict = Body(...)):
    try:
        url = _url_of(body)
        session = get_host().open(str(uuid.uuid4()), url)
        return JSONResponse({"ok": True, "session": session}, status_code=201)
    except BrowserError as err:
        return _fail(err)


@router.get("/{session_id}")
def one(session_id: str):
    try:
        host = get_host()
        session = host.session(session_id)
        return {"ok": True, **session, "running": host.is_running(session_id)}
    except BrowserError as err:
        return _fail(err)


@router.post("/{session_id}/open")
def reopen(session_id: str, body: dict = Body(default={})):
    """Open a closed window again (same profile, so it is still signed in)."""
    try:
        host = get_host()
        known = host.session(session_id)
        url = _url_of(body) if body.get("url") else (known["url"] or "about:blank")
        return {"ok": True, "session": host.open(session_id, url)}
    except BrowserError as err:
        return _fail(err)


@router.post("/{session_id}/navigate")
def navigate(session_id: str, body: dict = Body(...)):
    try:
        return {"ok": True, "session": get_host().navigate(session_id, _url_of(body))}
    except BrowserError as err:
        return _fail(err)


@router.post("/{session_id}/close")
def close(session_id: str):
    try:
        return {"ok": True, "session": get_host().close_session(session_id)}
    except BrowserError as err:
        return _fail(err)


@router.get("/{session_id}/read")
def read(session_id: str, elements: bool = False):
    try:
        return {"ok": True, **get_host().read(session_id, elements=elements)}
    except BrowserError as err:
        return _fail(err)


@router.get("/{session_id}/preview")
def preview(session_id: str):
    try:
        host = get_host()
        host.session(session_id)                      # 404 for one that never existed
        return Response(host.screenshot(session_id), media_type="image/png",
                        headers=_OUTSIDE_CONTENT)
    except BrowserError as err:
        return _fail(err)


@router.post("/{session_id}/input")
def send_input(session_id: str, body: dict = Body(...)):
    try:
        return {"ok": True, "session": get_host().input(session_id, body)}
    except BrowserError as err:
        return _fail(err)


@router.get("/{session_id}/console")
def console(session_id: str):
    try:
        validate_session_id(session_id)
        get_host().session(session_id)
    except BrowserError as err:
        return _fail(err)
    return HTMLResponse(console_page(session_id), headers={
        "Content-Security-Policy": CONSOLE_CSP, "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
