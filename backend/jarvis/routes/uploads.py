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
from fastapi.responses import JSONResponse

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
    return {"ok": True, "id": saved["id"], "name": saved["name"], "size": saved["size"]}


@router.get("/uploads/{upload_id}")
async def describe(upload_id: str):
    found = get_upload(upload_id)
    if found is None:
        return JSONResponse({"ok": False, "error": "Not found."}, status_code=404)
    # Deliberately not the path: see this module's docstring.
    return {"ok": True, "id": found["id"], "name": found["name"], "size": found["size"]}
