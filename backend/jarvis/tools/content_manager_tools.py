"""Content Management from Jarvis's side — the same actions the person has on
the screen, through the same lifecycle functions: hand content in (including a
file they attached in chat), change its text, schedule it, record numbers
reported for it, revise it, and say what is waiting.

**Never approve, publish, archive or delete.** Approving is the review itself,
and the other three can't be taken back by the person as easily as they were
made; those stay on the screen, and there is deliberately no tool for them.
Editing and scheduling change what may go public, so they are Risk.MEDIUM: the
person confirms in the conversation first.

**Why the two hand-in tools are LOW risk.** Neither changes anything outside
Jarvis or anything that cannot be undone: each only puts a draft in front of
the person in Review, where nothing moves on without their own approval, and a
revision keeps every earlier version. The review gate IS the confirmation. They
also have to run inside a Background Job (Jarvis revising something the person
sent back to it), where a MEDIUM tool would park the job waiting for a go-ahead
on a step that is itself only a request for the person's go-ahead.

Not `meta`: a job or a scheduled task may use these.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..capabilities import CapabilitySpec, Risk
from ..content_manager import files, lifecycle, store
from ..content_manager.kinds import FIELDS, PLATFORMS, TYPES, type_label

_FIELDS_SCHEMA = {
    "type": "object",
    "description": ("Supporting information. Keys: " + ", ".join(FIELDS) +
                    ". hashtags and tags are lists of words; everything else is text. "
                    "Only the fields the content type has are accepted."),
    "additionalProperties": True,
}


def _refused(err: Exception) -> dict[str, Any]:
    return {"ok": False, "error": str(err.args[0] if err.args else err)}


def _submit(name: str = "", content_type: str = "", niche: str = "", fields: Any = None,
            artifacts: Any = None, uploads: Any = None, **_: Any) -> dict[str, Any]:
    from ..artifacts import get as get_artifact
    from ..uploads import get_upload

    media, saved = [], []
    try:
        for entry in artifacts or []:
            artifact_id = entry.get("artifact_id") if isinstance(entry, dict) else str(entry)
            artifact = get_artifact(str(artifact_id or ""))
            if artifact is None or not artifact.path.exists():
                files.discard(saved)
                return {"ok": False, "error": f"There is no file with id {artifact_id}."}
            stored = files.save_path(artifact.path, artifact.name)
            saved.append(stored["fileId"])
            media.append({"fileId": stored["fileId"],
                          "role": (entry.get("role") if isinstance(entry, dict) else None) or "primary"})
        # A file the person attached in chat. Copied in: chat uploads are pruned
        # after a week, and content can wait longer than that to go out.
        for entry in uploads or []:
            upload_id = entry.get("upload_id") if isinstance(entry, dict) else str(entry)
            upload = get_upload(str(upload_id or ""))
            if upload is None:
                files.discard(saved)
                return {"ok": False, "error": f"There is no attached file with id {upload_id} any more."}
            stored = files.save_path(Path(upload["path"]), upload["name"])
            saved.append(stored["fileId"])
            media.append({"fileId": stored["fileId"],
                          "role": (entry.get("role") if isinstance(entry, dict) else None) or "primary"})
        item = lifecycle.submit(name=name, content_type=content_type, niche=niche, fields=fields,
                                media=media, producer="Jarvis")
    except (lifecycle.ContentError, lifecycle.NotFound, ValueError) as err:
        files.discard(saved)
        return _refused(err)
    return {"ok": True, "contentItemId": item["id"],
            "note": f"“{item['name']}” is waiting in Content → Review. Nothing is approved, scheduled "
                    "or published until they do it themselves."}


def _revise(content_item_id: str = "", fields: Any = None, note: str = "", **_: Any) -> dict[str, Any]:
    try:
        item = lifecycle.submit_revision(content_item_id, fields=fields, note=note, by="Jarvis")
    except (lifecycle.ContentError, lifecycle.NotFound) as err:
        return _refused(err)
    return {"ok": True, "revision": item["revision"],
            "note": f"Revision {item['revision']} of “{item['name']}” is back in Review for them to check."}


def _requests(assignee: str = "", **_: Any) -> dict[str, Any]:
    wanted = assignee if assignee in ("agent", "jarvis") else None
    out = []
    for request in store.open_change_requests(assignee=wanted):
        item = request.get("item") or {}
        out.append({"requestId": request["id"], "contentItemId": item.get("id"), "name": item.get("name"),
                    "type": type_label(item.get("contentType") or ""), "niche": item.get("niche"),
                    "what": request["what"], "why": request["why"], "sentTo": request["assignee"],
                    "status": request["status"], "currentFields": item.get("fields")})
    return {"ok": True, "requests": out}


def _placement_for(item: dict[str, Any], platform: str, destination: str = "") -> dict[str, Any] | None:
    matches = [p for p in item["placements"] if p["platform"] == platform]
    if destination:
        matches = [p for p in matches if p["destination"].lower() == destination.strip().lower()] or []
    return matches[0] if matches else None


def _edit(content_item_id: str = "", fields: Any = None, name: str | None = None, niche: str | None = None,
          **_: Any) -> dict[str, Any]:
    try:
        item = lifecycle.edit(content_item_id, name=name, niche=niche, fields=fields, by="Jarvis")
    except (lifecycle.ContentError, lifecycle.NotFound) as err:
        return _refused(err)
    return {"ok": True, "note": f"Saved the changes to “{item['name']}” (it's in {item['stage']})."}


def _schedule(content_item_id: str = "", platform: str = "", scheduled_at: str = "", timezone: str = "",
              destination: str = "", **_: Any) -> dict[str, Any]:
    try:
        item = store.get_item(content_item_id)
        if item is None:
            raise lifecycle.NotFound("That content item no longer exists.")
        placement = _placement_for(item, platform, destination)
        if placement is None:
            placement = lifecycle.add_placement(content_item_id, platform=platform, destination=destination,
                                                by="Jarvis")
        placement = lifecycle.schedule(placement["id"], scheduled_at=scheduled_at, timezone_name=timezone,
                                       by="Jarvis")
    except (lifecycle.ContentError, lifecycle.NotFound) as err:
        return _refused(err)
    return {"ok": True, "platform": placement["platformLabel"], "scheduledAtUtc": placement["scheduledAt"],
            "timezone": placement["timezone"],
            "note": f"“{item['name']}” is scheduled on {placement['platformLabel']}. It goes to whatever "
                    "publishes it when the time comes; it can still be changed or cancelled on the screen."}


def _metrics(content_item_id: str = "", platform: str = "", metrics: Any = None, captured_at: str = "",
             destination: str = "", **_: Any) -> dict[str, Any]:
    try:
        item = store.get_item(content_item_id)
        if item is None:
            raise lifecycle.NotFound("That content item no longer exists.")
        placement = _placement_for(item, platform, destination)
        if placement is None:
            raise lifecycle.ContentError(f"“{item['name']}” isn't going to {platform}.")
        lifecycle.record_metrics(placement["id"], metrics=metrics, captured_at=captured_at or None, by="Jarvis")
    except (lifecycle.ContentError, lifecycle.NotFound) as err:
        return _refused(err)
    return {"ok": True, "note": f"Recorded the {placement['platformLabel']} numbers for “{item['name']}”."}


def _status(**_: Any) -> dict[str, Any]:
    summary = store.summary()
    waiting = [{"name": i["name"], "type": i["typeLabel"], "niche": i["niche"], "needs": i["attention"],
                "next": i["next"]["step"]}
               for i in store.list_items() if i["attention"]]
    return {"ok": True, "counts": summary["counts"], "needsYou": summary["attention"], "items": waiting[:20]}


SPECS = [
    CapabilitySpec(
        id="builtin.submit_content_for_review", name="submit_content_for_review",
        description=("Hand a finished piece of publishable content to the owner's Content Management "
                     "Review queue. Use when you have produced a post, article, newsletter or caption "
                     "set they will publish. It only waits for their review — it never approves, "
                     "schedules or publishes. Files you made with create_artifact can be attached by id."),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "A short name to tell it apart in the list."},
                "content_type": {"type": "string", "enum": list(TYPES)},
                "niche": {"type": "string", "description": "The niche/topic area it belongs to, if known."},
                "fields": _FIELDS_SCHEMA,
                "artifacts": {"type": "array", "description": "Files to attach, from create_artifact.",
                              "items": {"type": "object", "properties": {
                                  "artifact_id": {"type": "string"},
                                  "role": {"type": "string", "enum": ["primary", "slide", "thumbnail",
                                                                      "cover", "attachment"]}},
                                  "required": ["artifact_id"]}},
                "uploads": {"type": "array",
                            "description": ("Files the owner attached in this chat, by their upload id (given in "
                                            "the note about attached files). 'primary' is the content itself."),
                            "items": {"type": "object", "properties": {
                                "upload_id": {"type": "string"},
                                "role": {"type": "string", "enum": ["primary", "slide", "thumbnail",
                                                                    "cover", "attachment"]}},
                                "required": ["upload_id"]}},
            },
            "required": ["name", "content_type"],
        },
        risk=Risk.LOW, handler=_submit, timeout_s=60.0,
    ),
    CapabilitySpec(
        id="builtin.submit_content_revision", name="submit_content_revision",
        description=("Hand back a revised version of a content item the owner asked changes for. Send "
                     "ONLY the fields you changed; the rest is kept. It goes back to their Review queue — "
                     "it never approves or publishes. Text only: video, images and audio can't be "
                     "changed here."),
        input_schema={
            "type": "object",
            "properties": {
                "content_item_id": {"type": "string"},
                "fields": _FIELDS_SCHEMA,
                "note": {"type": "string", "description": "One sentence: what you changed."},
            },
            "required": ["content_item_id", "fields"],
        },
        risk=Risk.LOW, handler=_revise, timeout_s=30.0,
    ),
    CapabilitySpec(
        id="builtin.list_content_change_requests", name="list_content_change_requests",
        description=("The owner's open change requests on their content: what needs to change and why, "
                     "and each item's current text. Use before revising, or for 'what changes did I ask for'."),
        input_schema={"type": "object", "properties": {
            "assignee": {"type": "string", "enum": ["agent", "jarvis"],
                         "description": "Only requests sent to this reviser."}}, "required": []},
        risk=Risk.LOW, handler=_requests, timeout_s=10.0,
    ),
    CapabilitySpec(
        id="builtin.content_status", name="content_status",
        description=("What is in the owner's Content Management right now: how many items are in each "
                     "stage (Review, Changes Requested, Ready to Post, Scheduling, Published, Archived, "
                     "Recycle Bin) and what needs them. Use for 'what's waiting for me', 'anything to "
                     "review', 'did my post go out'."),
        input_schema={"type": "object", "properties": {}, "required": []},
        risk=Risk.LOW, handler=_status, timeout_s=10.0,
    ),
    CapabilitySpec(
        id="builtin.edit_content_item", name="edit_content_item",
        description=("Change a content item's supporting text (title, caption, hashtags, description, body…), "
                     "its name or its niche — e.g. when the owner asks you to write or rewrite a caption. "
                     "Send ONLY what changes. Works from Review until the content has been published; a "
                     "scheduled post goes out with the change."),
        input_schema={
            "type": "object",
            "properties": {
                "content_item_id": {"type": "string"},
                "fields": _FIELDS_SCHEMA,
                "name": {"type": "string"},
                "niche": {"type": "string"},
            },
            "required": ["content_item_id"],
        },
        # Changes what may go public, so the owner confirms first.
        risk=Risk.MEDIUM, handler=_edit, timeout_s=15.0,
    ),
    CapabilitySpec(
        id="builtin.schedule_content", name="schedule_content",
        description=("Schedule an approved (Ready to Post) content item to go out on a platform at a time, "
                     "or move an existing schedule. Adds the platform to the item if it isn't there yet. "
                     "Give the time WITH its UTC offset and the timezone it was meant in. It never approves "
                     "anything and never posts by itself — the owner approves on the screen."),
        input_schema={
            "type": "object",
            "properties": {
                "content_item_id": {"type": "string"},
                "platform": {"type": "string", "enum": list(PLATFORMS)},
                "scheduled_at": {"type": "string",
                                 "description": "ISO 8601 with offset, e.g. 2026-10-02T09:00:00+01:00."},
                "timezone": {"type": "string", "description": "IANA zone, e.g. Europe/London."},
                "destination": {"type": "string", "description": "Where on the platform, if it matters."},
            },
            "required": ["content_item_id", "platform", "scheduled_at", "timezone"],
        },
        risk=Risk.MEDIUM, handler=_schedule, timeout_s=15.0,
    ),
    CapabilitySpec(
        id="builtin.record_content_metrics", name="record_content_metrics",
        description=("Record numbers REPORTED for a published post (views, likes, comments, shares, saves, "
                     "reach, impressions, watchTimeSeconds, engagement, or any other number the source "
                     "gave) — e.g. read from a platform or publishing tool. Only what was reported; never "
                     "estimate or calculate a number."),
        input_schema={
            "type": "object",
            "properties": {
                "content_item_id": {"type": "string"},
                "platform": {"type": "string", "enum": list(PLATFORMS)},
                "metrics": {"type": "object", "additionalProperties": {"type": "number"},
                            "description": "Name → number, e.g. {\"views\": 1200, \"likes\": 85}."},
                "captured_at": {"type": "string", "description": "When the numbers were true (ISO 8601 with "
                                                                  "offset). Leave out for now."},
                "destination": {"type": "string"},
            },
            "required": ["content_item_id", "platform", "metrics"],
        },
        risk=Risk.LOW, handler=_metrics, timeout_s=10.0,
    ),
]
