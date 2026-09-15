"""Files attached in conversation.

A raw body rather than a multipart form: this is one file at a time, and a
parser dependency for that alone is not a fair trade. FastAPI hands over the
bytes; the upload store writes them and returns an id.

**Returns an id, never a path.** The id goes to the browser and comes back on the
next turn, and a filesystem path making that round trip would be a gift to
anyone who could influence it. The store re-validates the id either way.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from ..artifacts.store import safe_name
from ..media import file_kind, mime_type_for
from ..uploads import get_upload, save_upload

router = APIRouter(prefix="/api")

#: Large enough for a real video, bounded so a runaway request cannot exhaust
#: memory: the body is read once and written straight out.
MAX_UPLOAD_BYTES = 512 * 1024 * 1024


@router.post("/uploads")
async def upload(request: Request, name: str = "upload"):
    body = await request.body()
    if len(body) > MAX_UPLOAD_BYTES:
        return JSONResponse({"ok": False, "error": "That file is too big to attach."},
                            status_code=413)
    try:
        saved = save_upload(body, name)
    except ValueError as err:
        return JSONResponse({"ok": False, "error": str(err)}, status_code=400)
    # `kind` ('image'|'video'|'audio'|'document'|'unknown') is the same
    # classification `attachments.py` uses to decide what the MODEL gets —
    # exposed here too so the browser can decide what to render in the
    # sender's own bubble without re-guessing it from a `File.type` that may
    # not agree.
    return {"ok": True, "id": saved["id"], "name": saved["name"], "size": saved["size"],
            "kind": file_kind(saved["name"])}


@router.get("/uploads/{upload_id}")
async def describe(upload_id: str):
    found = get_upload(upload_id)
    if found is None:
        return JSONResponse({"ok": False, "error": "Not found."}, status_code=404)
    # Deliberately not the path: see this module's docstring.
    return {"ok": True, "id": found["id"], "name": found["name"], "size": found["size"],
            "kind": file_kind(found["name"])}


@router.get("/uploads/{upload_id}/content")
def serve(upload_id: str):
    """The raw bytes back — for showing what someone attached in their own
    chat bubble. Nothing served this before: `describe()` above only ever
    returns metadata, on purpose (this module's own docstring), so this is a
    new surface and gets the same unconditional security headers
    `routes/artifacts.py` forces for exactly the same reason — nothing
    restricts what a user-attached file's bytes actually are, so nothing
    about how they're served may depend on a caller remembering to ask for
    safety. See that file's own header for the stored-XSS finding this
    pattern exists to close.
    """
    found = get_upload(upload_id)
    if found is None:
        return JSONResponse({"ok": False, "error": "Not found."}, status_code=404)

    filename = safe_name(found["name"], default="upload")
    return FileResponse(
        found["path"],
        media_type=mime_type_for(found["name"]),
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )
