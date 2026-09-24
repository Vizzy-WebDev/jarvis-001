"""Content Management from Jarvis's side: handing content in, revising it, and
saying what is waiting — never approving, scheduling, publishing or deleting.

Those four decisions belong to the person, on the Content screen, and there is
deliberately no tool here that can make any of them.

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

from typing import Any

from ..capabilities import CapabilitySpec, Risk
from ..content_manager import files, lifecycle, store
from ..content_manager.kinds import FIELDS, TYPES, type_label

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
            artifacts: Any = None, **_: Any) -> dict[str, Any]:
    from ..artifacts import get as get_artifact

    media, saved = [], []
    try:
        for entry in artifacts or []:
            artifact_id = entry.get("artifact_id") if isinstance(entry, dict) else str(entry)
            artifact = get_artifact(str(artifact_id or ""))
            if artifact is None or not artifact.path.exists():
                return {"ok": False, "error": f"There is no file with id {artifact_id}."}
            stored = files.save_path(artifact.path, artifact.name)
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
]
