"""Artifacts over HTTP: list, one item's details, a readable preview, the file, delete.

**`Content-Disposition: attachment` is unconditional on the file route, and that
is a security property rather than a preference.** The original forced a download
only when a `?download=1` query parameter was present, so a plain link, an
iframe, or a typed URL rendered the file INLINE in the app's own origin — and
since nothing restricts what an `.svg` or `.html` artifact contains, a crafted
file ran script in-app. A gate a caller can simply omit is not a gate.

Three more headers for the same reason: `nosniff` so the browser cannot decide
the content is something more interesting than we said; a sandboxing CSP as
defence in depth; and the filename stripped of CR/LF before it goes into a header
value, since an unsanitised filename injects response headers.

Viewing inside the app never goes through an inline route: the viewer FETCHES the
file's bytes (a download header does not stop `fetch`) and shows them itself —
text as text, images through `<img>`, a web page in a sandboxed frame. Word,
Excel and PowerPoint are shown from `/preview`, which is JSON (text and table
cells), never the file.

Only the person deletes. No capability calls `DELETE`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

from ..artifacts import KINDS, delete, get, list_page, safe_name
from ..artifacts.store import Artifact, conversations_of

router = APIRouter(prefix="/api")

MAX_PREVIEW_ROWS = 500


def _not_found() -> JSONResponse:
    # Exactly the recorded shape: the front end matches on it, and the contract
    # harness only catches unintended drift if intended responses stay
    # byte-identical too.
    return JSONResponse({"ok": False, "error": "Not found."}, status_code=404)


def _item(artifact: Artifact, conversations: dict[str, dict[str, Any]]) -> dict[str, Any]:
    out = artifact.as_result()
    out["conversation"] = conversations.get(artifact.conversation_id or "")
    return out


@router.get("/artifacts")
def list_artifacts(limit: int = 50, before: str = "", q: str = "", kind: str = "") -> dict:
    page, cursor = list_page(limit=limit, before=before or None, q=q or None,
                             kind=kind if kind in KINDS else None)
    conversations = conversations_of(page)
    body: dict[str, Any] = {"artifacts": [_item(a, conversations) for a in page]}
    if cursor:
        # Only when there is more: an empty or complete list keeps the shape the
        # contract recorded (`{"artifacts": []}`).
        body["nextBefore"] = cursor
    return body


@router.get("/artifacts/{artifact_id}/info")
def info(artifact_id: str):
    artifact = get(artifact_id)
    if artifact is None:
        return _not_found()
    return {"artifact": _item(artifact, conversations_of([artifact])),
            "exists": artifact.path.exists()}


@router.get("/artifacts/{artifact_id}/preview")
def preview(artifact_id: str):
    """A readable view of a Word, Excel or PowerPoint file, as JSON."""
    artifact = get(artifact_id)
    if artifact is None or not artifact.path.exists():
        return _not_found()
    kind = artifact.kind
    try:
        if kind in ("document", "presentation"):
            from ..documents import extract_document

            extracted = extract_document(artifact.path)
            return {"format": "markdown", "markdown": extracted.get("markdown") or "",
                    **({"note": extracted["note"]} if extracted.get("note") else {})}
        if kind == "spreadsheet":
            from ..documents import sheet_names, sheet_rows

            sheets, cut = [], False
            for name in sheet_names(artifact.path):
                rows = sheet_rows(artifact.path, name)
                cut = cut or len(rows) > MAX_PREVIEW_ROWS
                sheets.append({"name": name, "rows": rows[:MAX_PREVIEW_ROWS]})
            return {"format": "sheets", "sheets": sheets,
                    **({"note": f"Showing the first {MAX_PREVIEW_ROWS} rows of each sheet. "
                                "Download it to see everything."} if cut else {})}
    except Exception as err:  # noqa: BLE001 — say it could not be read, never crash
        return JSONResponse({"ok": False, "error": f"It couldn't be read for a preview: {err}"},
                            status_code=422)
    return JSONResponse({"ok": False, "error": "There is no preview for this kind of file."},
                        status_code=400)


@router.get("/artifacts/{artifact_id}")
def serve(artifact_id: str):
    artifact = get(artifact_id)
    if artifact is None or not artifact.path.exists():
        return _not_found()

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


@router.delete("/artifacts/{artifact_id}")
def remove(artifact_id: str):
    if not delete(artifact_id):
        return _not_found()
    return {"ok": True}
