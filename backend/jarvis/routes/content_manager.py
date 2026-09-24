"""Content Management over HTTP: the screen's routes and the agents' local API.

A surface over `jarvis/content_manager/` — every rule lives in `lifecycle.py`.
A refused move comes back as `{"ok": false, "error": "<plain sentence>"}` with a
400 (404 when the thing is gone), and the sentence is shown to the person as-is.

Media is served with `Content-Disposition: attachment`, `nosniff` and a sandbox
CSP, unconditionally — whatever an agent uploaded is content someone else wrote
(root CLAUDE.md). `<img>`, `<video>` and `<audio>` still play it in the screen.

Route order matters: the literal paths (`/summary`, `/calendar`, `/trash`) are
registered before `/{item_id}`, or they would be captured as an id.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Body, Request
from fastapi.responses import FileResponse, JSONResponse

from ..content_manager import files, lifecycle, revise, store
from ..db import get_db
from ..content_manager.kinds import meta as kinds_meta

router = APIRouter(prefix="/api")


def _fail(err: Exception):
    if isinstance(err, lifecycle.NotFound):
        return JSONResponse({"ok": False, "error": err.args[0] if err.args else "Not found."}, status_code=404)
    if isinstance(err, files.FileTooBig):
        return JSONResponse({"ok": False, "error": str(err)}, status_code=413)
    return JSONResponse({"ok": False, "error": str(err)}, status_code=400)


class _Refused(Exception):
    def __init__(self, response):
        super().__init__("refused")
        self.response = response


def _guard(handler):
    import functools
    import inspect

    if inspect.iscoroutinefunction(handler):
        @functools.wraps(handler)
        async def async_inner(*args, **kwargs):
            try:
                return await handler(*args, **kwargs)
            except _Refused as refused:
                return refused.response
            except (lifecycle.ContentError, lifecycle.NotFound, files.FileTooBig) as err:
                return _fail(err)
        return async_inner

    @functools.wraps(handler)
    def inner(*args, **kwargs):
        try:
            return handler(*args, **kwargs)
        except _Refused as refused:
            return refused.response
        except (lifecycle.ContentError, lifecycle.NotFound, files.FileTooBig) as err:
            return _fail(err)
    return inner


def _with_job(item: dict[str, Any] | None) -> dict[str, Any] | None:
    """Jarvis's revision job state, onto every request that has one."""
    if item is None:
        return None
    for request in [item.get("openRequest"), *(item.get("requests") or [])]:
        if request and request.get("jobId"):
            request["job"] = revise.job_state(request["jobId"])
    return item


def _filters(request: Request) -> dict[str, Any]:
    q = request.query_params
    return {"niche": q.get("niche") or None, "content_type": q.get("type") or None,
            "platform": q.get("platform") or None, "q": q.get("q") or None}


# --- what exists ------------------------------------------------------------------

@router.get("/content-meta")
def content_meta() -> dict[str, Any]:
    return {**kinds_meta(), "niches": store.niches(), "accounts": store.list_accounts()}


@router.get("/content-media/{file_id}")
def serve_media(file_id: str):
    found = files.get(file_id)
    if found is None:
        return JSONResponse({"ok": False, "error": "Not found."}, status_code=404)
    info, path = found
    return FileResponse(path, media_type=info["mime"], headers={
        "Content-Disposition": f'attachment; filename="{file_id}{path.suffix}"',
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
    })


# --- the lists ----------------------------------------------------------------------

@router.get("/content-items")
def list_items(request: Request) -> dict[str, Any]:
    q = request.query_params
    items = store.list_items(stage=q.get("stage") or None, date_from=q.get("from") or None,
                             date_to=q.get("to") or None, archived_from=q.get("archivedFrom") or None,
                             **_filters(request))
    return {"items": [_with_job(i) for i in items]}


@router.get("/content-items/summary")
def summary(request: Request) -> dict[str, Any]:
    return store.summary(**_filters(request))


@router.get("/content-items/calendar")
def calendar(request: Request):
    q = request.query_params
    start = store.normalize_time(q.get("start"))
    end = store.normalize_time(q.get("end"))
    if not start or not end:
        return JSONResponse({"ok": False, "error": "A calendar needs a start and an end time."}, status_code=400)
    return {"entries": store.calendar(start=start, end=end, **_filters(request))}


@router.delete("/content-items/trash")
@_guard
def empty_bin():
    return {"ok": True, "removed": lifecycle.empty_bin()}


# --- agents: handing content in -----------------------------------------------------------

async def _read_submission(request: Request) -> tuple[dict[str, Any], list[str]]:
    """The item description, from a JSON body or a multipart one (`item` plus
    files). A media entry may name an uploaded file by its form field or its
    filename (`"file": "clip.mp4"`); it is saved and replaced with its fileId.
    Returns the description and the ids of files saved here, so a refused
    submission can throw them away again."""
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith(("multipart/form-data", "application/x-www-form-urlencoded")):
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            raise _Refused(JSONResponse({"ok": False, "error": "Send the item as JSON, or as multipart with an "
                                                              "“item” field."}, status_code=400))
        if not isinstance(body, dict):
            raise _Refused(JSONResponse({"ok": False, "error": "The item must be a JSON object."}, status_code=400))
        return body, []

    form = await request.form(max_files=200, max_part_size=4 * 1024 * 1024)
    try:
        body = json.loads(str(form.get("item") or "{}"))
    except json.JSONDecodeError:
        raise _Refused(JSONResponse({"ok": False, "error": "The “item” field isn't valid JSON."}, status_code=400))
    if not isinstance(body, dict):
        raise _Refused(JSONResponse({"ok": False, "error": "The “item” field must be a JSON object."},
                                    status_code=400))
    uploads = {}
    for key, value in form.multi_items():
        if key != "item" and hasattr(value, "filename") and hasattr(value, "file"):
            uploads[key] = value
            if value.filename:
                uploads.setdefault(value.filename, value)
    saved: dict[int, str] = {}
    saved_ids: list[str] = []
    try:
        for entry in body.get("media") or []:
            if not isinstance(entry, dict) or entry.get("fileId") or not entry.get("file"):
                continue
            upload = uploads.get(str(entry["file"]))
            if upload is None:
                raise lifecycle.ContentError(f"“{entry['file']}” is listed in media but wasn't uploaded.")
            if id(upload) not in saved:
                stored = files.save_stream(upload.file, upload.filename or str(entry["file"]))
                saved[id(upload)] = stored["fileId"]
                saved_ids.append(stored["fileId"])
            entry["fileId"] = saved[id(upload)]
    except BaseException:
        files.discard(saved_ids)
        raise
    finally:
        await form.close()
    return body, saved_ids


@router.post("/content-files")
@_guard
async def upload_files(request: Request):
    """Files on their own, for an agent that prefers to upload first and then
    refer to them by `fileId` in the item."""
    form = await request.form(max_files=200)
    out = []
    try:
        for key, value in form.multi_items():
            if hasattr(value, "filename") and hasattr(value, "file"):
                out.append(files.save_stream(value.file, value.filename or key))
    except BaseException:
        files.discard([f["fileId"] for f in out])
        raise
    finally:
        await form.close()
    if not out:
        return JSONResponse({"ok": False, "error": "No files were sent."}, status_code=400)
    return {"ok": True, "files": out}


@router.post("/content-items")
@_guard
async def submit(request: Request):
    body, saved = await _read_submission(request)
    try:
        item = lifecycle.submit(
            name=str(body.get("name") or ""), content_type=str(body.get("contentType") or ""),
            niche=str(body.get("niche") or ""), fields=body.get("fields"), media=body.get("media"),
            findings=body.get("findings"), producer=str(body.get("producer") or ""),
            platforms=body.get("platforms"))
    except BaseException:
        files.discard(saved)
        raise
    return {"ok": True, "item": item}


@router.post("/content-items/{item_id}/revisions")
@_guard
async def submit_revision(item_id: str, request: Request):
    body, saved = await _read_submission(request)
    try:
        item = lifecycle.submit_revision(item_id, fields=body.get("fields"), media=body.get("media"),
                                         note=str(body.get("note") or ""), by=str(body.get("by") or ""))
    except BaseException:
        files.discard(saved)
        raise
    return {"ok": True, "item": item}


@router.get("/content-change-requests")
def change_requests(status: str | None = None, assignee: str | None = None) -> dict[str, Any]:
    return {"requests": store.open_change_requests(status, assignee)}


@router.post("/content-change-requests/{request_id}/pick-up")
@_guard
def pick_up(request_id: str, body: dict[str, Any] = Body(default_factory=dict)):
    return {"ok": True, "request": lifecycle.pick_up(request_id, by=str(body.get("by") or ""))}


@router.patch("/content-change-requests/{request_id}")
@_guard
def update_request(request_id: str, body: dict[str, Any] = Body(default_factory=dict)):
    updated = lifecycle.update_request(
        request_id, what=body.get("what"), why=body.get("why"), assignee=body.get("assignee"))
    if body.get("assignee") == "jarvis":
        revise.start_jarvis_revision(request_id)
    return {"ok": True, "item": _with_job(store.item_detail(updated["itemId"]))}


@router.post("/content-change-requests/{request_id}/cancel")
@_guard
def cancel_request(request_id: str):
    return {"ok": True, "item": lifecycle.cancel_request(request_id)}


@router.post("/content-change-requests/{request_id}/start-jarvis")
@_guard
def retry_jarvis(request_id: str):
    job = revise.start_jarvis_revision(request_id)
    row = get_db().execute("SELECT item_id FROM cm_change_requests WHERE id = ?", (request_id,)).fetchone()
    if row is None:
        return JSONResponse({"ok": False, "error": "That change request no longer exists."}, status_code=404)
    return {"ok": job is not None, "item": _with_job(store.item_detail(row["item_id"]))}


# --- publishers ---------------------------------------------------------------------------

@router.get("/content-publish-queue")
def publish_queue() -> dict[str, Any]:
    return {"queue": store.publish_queue()}


@router.post("/content-placements/{placement_id}/claim")
@_guard
def claim(placement_id: str, body: dict[str, Any] = Body(default_factory=dict)):
    return {"ok": True, "placement": lifecycle.claim(placement_id, by=str(body.get("by") or ""))}


@router.post("/content-placements/{placement_id}/result")
@_guard
def result(placement_id: str, body: dict[str, Any] = Body(default_factory=dict)):
    if not isinstance(body.get("ok"), bool):
        return JSONResponse({"ok": False, "error": "Say whether it worked: “ok”: true or false."}, status_code=400)
    return {"ok": True, "placement": lifecycle.report_result(
        placement_id, ok=body["ok"], url=str(body.get("url") or ""), error=str(body.get("error") or ""),
        by=str(body.get("by") or ""))}


# --- one item (the person) ----------------------------------------------------------------

@router.get("/content-items/{item_id}")
def item_detail(item_id: str):
    item = store.item_detail(item_id)
    if item is None:
        return JSONResponse({"ok": False, "error": "That content item no longer exists."}, status_code=404)
    return {"item": _with_job(item)}


@router.patch("/content-items/{item_id}")
@_guard
def edit(item_id: str, body: dict[str, Any] = Body(default_factory=dict)):
    item = lifecycle.edit(item_id, name=body.get("name"), niche=body.get("niche"), fields=body.get("fields"))
    return {"ok": True, "item": _with_job(item)}


@router.post("/content-items/{item_id}/approve")
@_guard
def approve(item_id: str):
    return {"ok": True, "item": lifecycle.approve(item_id)}


@router.post("/content-items/{item_id}/request-changes")
@_guard
def request_changes(item_id: str, body: dict[str, Any] = Body(default_factory=dict)):
    request = lifecycle.request_changes(item_id, what=str(body.get("what") or ""), why=str(body.get("why") or ""),
                                        assignee=str(body.get("assignee") or "agent"))
    if request["assignee"] == "jarvis":
        revise.start_jarvis_revision(request["id"])
    return {"ok": True, "item": _with_job(store.get_item(item_id))}


@router.post("/content-items/{item_id}/placements")
@_guard
def add_placement(item_id: str, body: dict[str, Any] = Body(default_factory=dict)):
    placement = lifecycle.add_placement(item_id, platform=str(body.get("platform") or ""),
                                        account_id=body.get("accountId") or None,
                                        destination=str(body.get("destination") or ""))
    return {"ok": True, "placement": placement, "item": _with_job(store.get_item(item_id))}


@router.post("/content-items/{item_id}/archive")
@_guard
def archive(item_id: str):
    return {"ok": True, "item": lifecycle.archive(item_id)}


@router.post("/content-items/{item_id}/unarchive")
@_guard
def unarchive(item_id: str):
    return {"ok": True, "item": lifecycle.unarchive(item_id)}


@router.post("/content-items/{item_id}/restore")
@_guard
def restore(item_id: str):
    return {"ok": True, "item": lifecycle.restore(item_id)}


@router.delete("/content-items/{item_id}/permanent")
@_guard
def purge(item_id: str):
    lifecycle.purge(item_id)
    return {"ok": True}


@router.delete("/content-items/{item_id}")
@_guard
def delete(item_id: str):
    return {"ok": True, "item": lifecycle.delete(item_id)}


# --- one placement (the person) --------------------------------------------------------------

@router.patch("/content-placements/{placement_id}")
@_guard
def update_placement(placement_id: str, body: dict[str, Any] = Body(default_factory=dict)):
    kwargs: dict[str, Any] = {"overrides": body.get("overrides"), "destination": body.get("destination")}
    if "accountId" in body:
        kwargs["account_id"] = body.get("accountId") or None
    return {"ok": True, "placement": lifecycle.update_placement(placement_id, **kwargs)}


@router.delete("/content-placements/{placement_id}")
@_guard
def remove_placement(placement_id: str):
    return {"ok": True, "item": lifecycle.remove_placement(placement_id)}


@router.post("/content-placements/{placement_id}/schedule")
@_guard
def schedule(placement_id: str, body: dict[str, Any] = Body(default_factory=dict)):
    return {"ok": True, "placement": lifecycle.schedule(
        placement_id, scheduled_at=body.get("scheduledAt"), timezone_name=str(body.get("timezone") or ""))}


@router.post("/content-placements/{placement_id}/unschedule")
@_guard
def unschedule(placement_id: str):
    return {"ok": True, "item": lifecycle.unschedule(placement_id)}


@router.post("/content-placements/{placement_id}/post-now")
@_guard
def post_now(placement_id: str):
    return {"ok": True, "item": lifecycle.post_now(placement_id)}


@router.post("/content-placements/{placement_id}/requeue")
@_guard
def requeue(placement_id: str):
    return {"ok": True, "item": lifecycle.requeue(placement_id)}


@router.post("/content-placements/{placement_id}/mark-posted")
@_guard
def mark_posted(placement_id: str, body: dict[str, Any] = Body(default_factory=dict)):
    return {"ok": True, "item": lifecycle.mark_posted(placement_id, url=str(body.get("url") or ""))}


# --- accounts ------------------------------------------------------------------------------

@router.get("/content-accounts")
def accounts() -> dict[str, Any]:
    return {"accounts": store.list_accounts()}


@router.post("/content-accounts")
@_guard
def create_account(body: dict[str, Any] = Body(default_factory=dict)):
    return {"ok": True, "account": lifecycle.create_account(
        platform=str(body.get("platform") or ""), handle=str(body.get("handle") or ""),
        destinations=body.get("destinations") if isinstance(body.get("destinations"), list) else [],
        default_niche=str(body.get("defaultNiche") or ""))}


@router.patch("/content-accounts/{account_id}")
@_guard
def update_account(account_id: str, body: dict[str, Any] = Body(default_factory=dict)):
    return {"ok": True, "account": lifecycle.update_account(
        account_id, handle=body.get("handle"),
        destinations=body.get("destinations") if isinstance(body.get("destinations"), list) else None,
        default_niche=body.get("defaultNiche"))}


@router.delete("/content-accounts/{account_id}")
@_guard
def delete_account(account_id: str):
    lifecycle.delete_account(account_id)
    return {"ok": True}
