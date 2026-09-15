"""Serving a generated file back.

**`Content-Disposition: attachment` is unconditional here, and that is a
security property rather than a preference.** The original forced a download only
when a `?download=1` query parameter was present, so a plain link, an iframe, or
a typed URL rendered the file INLINE in the app's own origin — and since nothing
restricts what an `.svg` or `.html` artifact contains, a crafted file ran script
in-app. A gate a caller can simply omit is not a gate.

Three more headers for the same reason: `nosniff` so the browser cannot decide
the content is something more interesting than we said; a sandboxing CSP as
defence in depth; and the filename stripped of CR/LF before it goes into a header
value, since an unsanitised filename injects response headers.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

from ..artifacts import get, recent, safe_name

router = APIRouter(prefix="/api")


@router.get("/artifacts")
def list_artifacts() -> dict:
    return {"artifacts": [a.as_result() for a in recent()]}


@router.get("/artifacts/{artifact_id}")
def serve(artifact_id: str):
    artifact = get(artifact_id)
    if artifact is None or not artifact.path.exists():
        # Exactly the recorded shape: the front end matches on it, and the
        # contract harness only catches unintended drift if intended responses
        # stay byte-identical too.
        return JSONResponse({"ok": False, "error": "Not found."}, status_code=404)

    filename = safe_name(artifact.name)
    return FileResponse(
        artifact.path,
        media_type=artifact.mime_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )
