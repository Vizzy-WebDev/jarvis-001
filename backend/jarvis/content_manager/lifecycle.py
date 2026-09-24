"""Every change to a content item goes through here.

One module owns the lifecycle so it is answerable in one place what can move
where, and so every move is recorded the same way: validated against the item's
current state, written, logged to its history, and announced on the event bus
(which is what makes an open Content screen refresh itself).

A refused move raises `ContentError` with a sentence fit to show the person —
the routes surface it as-is.

Stages: review → changes_requested ⇄ review → approved → scheduling → published
→ archived. After approval an item's stage is DERIVED from its placements
(`_derive`), never set by hand: anything scheduled, queued or being posted means
Scheduling; otherwise anything published means Published; otherwise Ready to
Post. The recycle bin is `deleted_at`, separate from the stage, so a restore
returns an item exactly where it was.

Nothing here approves, schedules or publishes on its own. The same functions
serve everyone — the screen, Jarvis's tools and agents over HTTP. Jarvis's tools
call `submit`, `submit_revision`, `edit`, `schedule` (those two after the person
confirms) and `record_metrics`; approving, archiving and deleting are only ever
the person, through the screen.
"""

from __future__ import annotations

import re
import threading
import uuid
from typing import Any

from ..db import get_db
from ..events import EventType, bus as default_bus
from ..jscompat import now_iso
from . import files
from .kinds import ASSETS, PLATFORMS, TYPES, platform_label, type_label
from .store import PENDING, _loads as store_loads, dumps, editable, get_item, normalize_time, request_dict

_lock = threading.RLock()

POST_APPROVAL = ("approved", "scheduling", "published")
MEDIA_ROLES = ("primary", "slide", *ASSETS)
YOU = "you"


class ContentError(ValueError):
    """A move that cannot happen, in words fit to show the person."""


class NotFound(KeyError):
    pass


# --- plumbing --------------------------------------------------------------------

def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _tx(work):
    """Run `work(db)` in one transaction, under this module's lock."""
    with _lock:
        db = get_db()
        db.execute("BEGIN")
        try:
            result = work(db)
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        return result


def _event(db, item_id: str, actor: str, kind: str, note: str = "") -> None:
    db.execute("INSERT INTO cm_events (item_id, at, actor, kind, note) VALUES (?,?,?,?,?)",
               (item_id, now_iso(), actor or YOU, kind, note or ""))


def _announce(item_id: str, event_bus: Any = None) -> dict[str, Any] | None:
    item = get_item(item_id)
    (event_bus or default_bus).publish(
        EventType.CONTENT_CHANGED,
        {"id": item_id, "stage": item["stage"] if item else None,
         "deleted": bool(item and item["deletedAt"])})
    return item


def _notify(title: str, body: str, item_id: str, level: str = "info", event_bus: Any = None) -> None:
    (event_bus or default_bus).publish(EventType.NOTIFICATION_CREATED, {
        "kind": "content", "level": level, "title": title, "body": body,
        "action": {"label": "Open Content", "section": "content"},
        "meta": {"contentItemId": item_id}})


def _row(db, item_id: str):
    row = db.execute("SELECT * FROM cm_items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise NotFound("That content item no longer exists.")
    return row


def _live(db, item_id: str):
    """The item, refusing one that is in the recycle bin."""
    row = _row(db, item_id)
    if row["deleted_at"]:
        raise ContentError("That item is in the Recycle Bin — restore it first.")
    return row


def _placement(db, placement_id: str):
    row = db.execute("SELECT * FROM cm_placements WHERE id = ?", (placement_id,)).fetchone()
    if row is None:
        raise NotFound("That post no longer exists.")
    return row


def _derive(db, item_id: str) -> str:
    """Recompute an approved item's stage from its placements, and store it."""
    row = _row(db, item_id)
    if row["stage"] not in POST_APPROVAL:
        return row["stage"]
    statuses = [r["status"] for r in db.execute(
        "SELECT status FROM cm_placements WHERE item_id = ?", (item_id,))]
    if any(s in PENDING for s in statuses):
        stage = "scheduling"
    elif "published" in statuses:
        stage = "published"
    else:
        stage = "approved"
    if stage != row["stage"]:
        db.execute("UPDATE cm_items SET stage = ?, updated_at = ? WHERE id = ?",
                   (stage, now_iso(), item_id))
    return stage


def _touch(db, item_id: str) -> None:
    db.execute("UPDATE cm_items SET updated_at = ? WHERE id = ?", (now_iso(), item_id))


# --- validating what an agent hands in -------------------------------------------

def _clean_fields(content_type: str, fields: Any, *, partial: bool = False) -> dict[str, Any]:
    if fields is None:
        return {}
    if not isinstance(fields, dict):
        raise ContentError("“fields” must be an object of supporting information.")
    allowed = TYPES[content_type]["fields"]
    out: dict[str, Any] = {}
    for key, value in fields.items():
        if key not in allowed:
            label = type_label(content_type)
            raise ContentError(f"A {label} doesn't have a “{key}”. It can have: {', '.join(allowed)}.")
        if key in ("hashtags", "tags"):
            if isinstance(value, str):
                value = [v for v in value.replace(",", " ").split() if v] if key == "hashtags" \
                    else [v.strip() for v in value.split(",") if v.strip()]
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ContentError(f"“{key}” must be a list of words.")
            out[key] = [v.strip() for v in value if v.strip()]
        else:
            if value is not None and not isinstance(value, str):
                raise ContentError(f"“{key}” must be text.")
            out[key] = value or ""
    return out


def _clean_media(content_type: str, media: Any, *, item_id: str | None) -> list[dict[str, Any]]:
    if media is None:
        return []
    if not isinstance(media, list):
        raise ContentError("“media” must be a list.")
    out = []
    for index, entry in enumerate(media):
        if not isinstance(entry, dict) or not entry.get("fileId"):
            raise ContentError("Every media entry needs a fileId.")
        role = entry.get("role") or "primary"
        if role not in MEDIA_ROLES:
            raise ContentError(f"“{role}” isn't a media role. Use one of: {', '.join(MEDIA_ROLES)}.")
        file_id = str(entry["fileId"])
        if not (files.is_unowned(file_id) or (item_id and files.owned_by([file_id], item_id))):
            raise ContentError(f"File {file_id} doesn't exist, or belongs to something else.")
        out.append({"fileId": file_id, "role": role, "order": int(entry.get("order") or index)})
    return out


def _check_content_present(content_type: str, fields: dict[str, Any], media: list[dict[str, Any]]) -> None:
    kind = TYPES[content_type]
    label = kind["label"]
    if kind["media"] is None:
        if not (fields.get("body") or "").strip():
            raise ContentError(f"A {label} needs its text (“body”).")
    elif not any(m["role"] == kind["media"] for m in media):
        what = "at least one slide" if kind["media"] == "slide" else "the actual file"
        raise ContentError(f"A {label} needs {what} — only its supporting information was sent.")


def _clean_findings(findings: Any) -> list[dict[str, str]]:
    if not findings:
        return []
    if not isinstance(findings, list):
        raise ContentError("“findings” must be a list.")
    out = []
    for f in findings:
        if isinstance(f, str):
            out.append({"level": "note", "text": f})
        elif isinstance(f, dict) and f.get("text"):
            level = f.get("level") if f.get("level") in ("note", "warning", "problem") else "note"
            out.append({"level": level, "text": str(f["text"])})
    return out


def _clean_platform(content_type: str, platform: str) -> str:
    if platform not in PLATFORMS:
        raise ContentError(f"“{platform}” isn't a platform I know.")
    if content_type not in PLATFORMS[platform]["accepts"]:
        raise ContentError(f"{platform_label(platform)} doesn't take a {type_label(content_type)}.")
    return platform


# --- producing (agents) --------------------------------------------------------------

def submit(*, name: str, content_type: str, niche: str = "", fields: Any = None, media: Any = None,
           findings: Any = None, producer: str = "", platforms: Any = None, approve_now: bool = False,
           event_bus: Any = None) -> dict[str, Any]:
    """A finished piece of content, handed in — by an agent, by Jarvis, or by the
    person uploading it on the screen; all the same door.

    `approve_now` is the person choosing "Ready to Post" as they add it: the same
    submit, then the ordinary `approve()`, recorded the same way. Only the screen
    offers it — approving is the person's, and no Jarvis tool passes it.
    """
    name = (name or "").strip()
    if not name:
        raise ContentError("Give the content a name, so it can be told apart in the list.")
    if content_type not in TYPES:
        raise ContentError(f"“{content_type}” isn't a content type. Use one of: {', '.join(TYPES)}.")
    clean_fields = _clean_fields(content_type, fields)
    clean_media = _clean_media(content_type, media, item_id=None)
    _check_content_present(content_type, clean_fields, clean_media)
    clean_findings = _clean_findings(findings)
    targets = []
    for entry in platforms or []:
        platform = entry.get("platform") if isinstance(entry, dict) else str(entry)
        targets.append((_clean_platform(content_type, str(platform)),
                        (entry.get("destination") or "") if isinstance(entry, dict) else ""))
    producer = (producer or "").strip()[:80]
    item_id = _id("ci")
    stamp = now_iso()

    def work(db):
        db.execute(
            "INSERT INTO cm_items (id, name, content_type, niche, stage, producer, revision, fields_json, "
            "media_json, findings_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (item_id, name[:200], content_type, (niche or "").strip()[:80], "review", producer, 1,
             dumps(clean_fields), dumps(clean_media), dumps(clean_findings), stamp, stamp))
        files.claim_for_item([m["fileId"] for m in clean_media], item_id)
        db.execute("INSERT INTO cm_revisions (item_id, revision, fields_json, media_json, by, note, "
                   "created_at) VALUES (?,?,?,?,?,?,?)",
                   (item_id, 1, dumps(clean_fields), dumps(clean_media), producer, "First version", stamp))
        for platform, destination in targets:
            db.execute("INSERT INTO cm_placements (id, item_id, platform, destination, created_at, "
                       "updated_at) VALUES (?,?,?,?,?,?)",
                       (_id("cp"), item_id, platform, destination[:120], stamp, stamp))
        _event(db, item_id, producer or "agent", "submitted", "Handed in for review")

    _tx(work)
    if approve_now:
        return approve(item_id, event_bus=event_bus)
    _notify(f"New content to review: {name}",
            f"{type_label(content_type)}{' · ' + niche if niche else ''} from {producer or 'an agent'}.",
            item_id, event_bus=event_bus)
    return _announce(item_id, event_bus)


def pick_up(request_id: str, *, by: str, event_bus: Any = None) -> dict[str, Any]:
    """An agent saying "I'm working on this change request now"."""
    by = (by or "").strip()[:80] or "agent"

    def work(db):
        row = db.execute("SELECT * FROM cm_change_requests WHERE id = ?", (request_id,)).fetchone()
        if row is None:
            raise NotFound("That change request no longer exists.")
        _live(db, row["item_id"])
        if row["status"] not in ("open", "in_progress"):
            raise ContentError("That change request is already closed.")
        if row["status"] == "in_progress" and row["picked_up_by"] not in (None, by):
            raise ContentError(f"{row['picked_up_by']} is already working on it.")
        db.execute("UPDATE cm_change_requests SET status = 'in_progress', picked_up_by = ?, "
                   "picked_up_at = COALESCE(picked_up_at, ?), start_error = NULL WHERE id = ?",
                   (by, now_iso(), request_id))
        _event(db, row["item_id"], by, "revision_started", "Started working on the requested changes")
        _touch(db, row["item_id"])
        return row["item_id"]

    item_id = _tx(work)
    _announce(item_id, event_bus)
    row = get_db().execute("SELECT * FROM cm_change_requests WHERE id = ?", (request_id,)).fetchone()
    return request_dict(row)


def submit_revision(item_id: str, *, fields: Any = None, media: Any = None, note: str = "",
                    by: str = "", event_bus: Any = None) -> dict[str, Any]:
    """A revised version, answering the open change request. Back to Review."""
    by = (by or "").strip()[:80] or "agent"

    def work(db):
        row = _live(db, item_id)
        if row["stage"] != "changes_requested":
            raise ContentError("Nobody asked for changes to that item, so there is nothing to revise.")
        content_type = row["content_type"]
        patch = _clean_fields(content_type, fields)
        merged = {**store_loads(row["fields_json"], {}), **patch}
        new_media = _clean_media(content_type, media, item_id=item_id) if media is not None \
            else store_loads(row["media_json"], [])
        _check_content_present(content_type, merged, new_media)
        if not patch and media is None:
            raise ContentError("A revision has to change something — no fields or media were sent.")
        revision = row["revision"] + 1
        stamp = now_iso()
        files.claim_for_item([m["fileId"] for m in new_media], item_id)
        db.execute("UPDATE cm_items SET fields_json = ?, media_json = ?, revision = ?, stage = 'review', "
                   "updated_at = ? WHERE id = ?",
                   (dumps(merged), dumps(new_media), revision, stamp, item_id))
        db.execute("INSERT INTO cm_revisions (item_id, revision, fields_json, media_json, by, note, "
                   "created_at) VALUES (?,?,?,?,?,?,?)",
                   (item_id, revision, dumps(merged), dumps(new_media), by, (note or "")[:1000], stamp))
        db.execute("UPDATE cm_change_requests SET status = 'resolved', resolved_at = ?, "
                   "resolved_revision = ? WHERE item_id = ? AND status IN ('open','in_progress')",
                   (stamp, revision, item_id))
        _event(db, item_id, by, "revision_submitted", note or f"Revision {revision} handed in")
        return row["name"], revision

    name, revision = _tx(work)
    _notify(f"Revision ready: {name}", f"Revision {revision} is back for your review.", item_id,
            event_bus=event_bus)
    return _announce(item_id, event_bus)


# --- reviewing (the person) --------------------------------------------------------------

def edit(item_id: str, *, name: str | None = None, niche: str | None = None, fields: Any = None,
         media: Any = None, by: str = YOU, event_bus: Any = None) -> dict[str, Any]:
    """Change the content itself: its supporting text and/or its files (`media`,
    the full new list). Allowed from Review until it has gone out
    (`store.editable`) — a scheduled post is still a plan, so the change goes out
    with it. Once any platform has published, the change becomes a NEW revision,
    so the record of what already went out is never rewritten."""
    def work(db):
        row = _live(db, item_id)
        changes = []
        if name is not None:
            if not name.strip():
                raise ContentError("The name can't be empty.")
            db.execute("UPDATE cm_items SET name = ? WHERE id = ?", (name.strip()[:200], item_id))
            changes.append("name")
        if niche is not None:
            db.execute("UPDATE cm_items SET niche = ? WHERE id = ?", (niche.strip()[:80], item_id))
            changes.append("niche")
        if fields is not None or media is not None:
            statuses = [r["status"] for r in db.execute(
                "SELECT status FROM cm_placements WHERE item_id = ?", (item_id,))]
            if not editable(row["stage"], statuses):
                if any(s in ("queued", "publishing") for s in statuses):
                    raise ContentError("A post of this is on its way to a platform right now — wait for it "
                                       "to finish, or cancel it first.")
                raise ContentError("The content can be changed from Review until it has been published.")
            patch = _clean_fields(row["content_type"], fields) if fields is not None else {}
            merged = {**store_loads(row["fields_json"], {}), **patch}
            new_media = (_clean_media(row["content_type"], media, item_id=item_id) if media is not None
                         else store_loads(row["media_json"], []))
            _check_content_present(row["content_type"], merged, new_media)
            files.claim_for_item([m["fileId"] for m in new_media], item_id)
            db.execute("UPDATE cm_items SET fields_json = ?, media_json = ? WHERE id = ?",
                       (dumps(merged), dumps(new_media), item_id))
            if "published" in statuses:
                # What already went out stays on record as the revision it was.
                revision = row["revision"] + 1
                db.execute("UPDATE cm_items SET revision = ? WHERE id = ?", (revision, item_id))
                db.execute("INSERT INTO cm_revisions (item_id, revision, fields_json, media_json, by, note, "
                           "created_at) VALUES (?,?,?,?,?,?,?)",
                           (item_id, revision, dumps(merged), dumps(new_media), by, "Edited after publishing",
                            now_iso()))
            else:
                # The revision on record is the version as it stands — the
                # person's own edits included — not the first draft.
                db.execute("UPDATE cm_revisions SET fields_json = ?, media_json = ? WHERE item_id = ? "
                           "AND revision = ?", (dumps(merged), dumps(new_media), item_id, row["revision"]))
            changes += sorted(patch)
            if media is not None:
                changes.append("files")
        if changes:
            _touch(db, item_id)
            _event(db, item_id, by, "edited", "Edited " + ", ".join(changes))

    _tx(work)
    return _announce(item_id, event_bus)


def approve(item_id: str, *, event_bus: Any = None) -> dict[str, Any]:
    def work(db):
        row = _live(db, item_id)
        if row["stage"] != "review":
            raise ContentError("Only something in Review can be approved.")
        stamp = now_iso()
        db.execute("UPDATE cm_items SET stage = 'approved', approved_at = ?, updated_at = ? WHERE id = ?",
                   (stamp, stamp, item_id))
        _event(db, item_id, YOU, "approved", f"Approved revision {row['revision']}")
        _derive(db, item_id)

    _tx(work)
    return _announce(item_id, event_bus)


def _cancel_pending(db, item_id: str, why: str) -> int:
    """Take every scheduled/queued post back to a draft. Refuses while one is
    actually being posted — that cannot be called back from here."""
    rows = db.execute("SELECT * FROM cm_placements WHERE item_id = ?", (item_id,)).fetchall()
    if any(r["status"] == "publishing" for r in rows):
        raise ContentError("A post of this is being published right now — wait for it to finish.")
    cancelled = 0
    for r in rows:
        if r["status"] in ("scheduled", "queued"):
            db.execute("UPDATE cm_placements SET status = 'draft', scheduled_at = NULL, updated_at = ? "
                       "WHERE id = ?", (now_iso(), r["id"]))
            _event(db, item_id, YOU, "unscheduled", f"{platform_label(r['platform'])} schedule cancelled — {why}")
            cancelled += 1
    return cancelled


def request_changes(item_id: str, *, what: str, why: str = "", assignee: str = "agent",
                    event_bus: Any = None) -> dict[str, Any]:
    what = (what or "").strip()
    if not what:
        raise ContentError("Say what needs to change.")
    if assignee not in ("agent", "jarvis"):
        raise ContentError("Changes can go to the agent that made it, or to Jarvis.")

    def work(db):
        row = _live(db, item_id)
        if row["stage"] not in ("review", "approved", "scheduling"):
            if row["stage"] == "published":
                raise ContentError("It's already published — changes can't be requested any more.")
            raise ContentError("Changes can be requested from Review or Ready to Post.")
        if db.execute("SELECT 1 FROM cm_placements WHERE item_id = ? AND status = 'published'",
                      (item_id,)).fetchone():
            raise ContentError("It's already published somewhere — changes can't be requested any more.")
        _cancel_pending(db, item_id, "changes were requested")
        request_id = _id("cr")
        stamp = now_iso()
        db.execute("INSERT INTO cm_change_requests (id, item_id, revision, what, why, assignee, status, "
                   "created_at) VALUES (?,?,?,?,?,?,'open',?)",
                   (request_id, item_id, row["revision"], what[:4000], (why or "").strip()[:4000],
                    assignee, stamp))
        db.execute("UPDATE cm_items SET stage = 'changes_requested', approved_at = NULL, updated_at = ? "
                   "WHERE id = ?", (stamp, item_id))
        who = "Jarvis" if assignee == "jarvis" else (row["producer"] or "the agent")
        _event(db, item_id, YOU, "changes_requested", f"{what} — sent to {who}")
        return request_id

    request_id = _tx(work)
    _announce(item_id, event_bus)
    return request_dict(get_db().execute("SELECT * FROM cm_change_requests WHERE id = ?",
                                         (request_id,)).fetchone())


def update_request(request_id: str, *, what: str | None = None, why: str | None = None,
                   assignee: str | None = None, event_bus: Any = None) -> dict[str, Any]:
    def work(db):
        row = db.execute("SELECT * FROM cm_change_requests WHERE id = ?", (request_id,)).fetchone()
        if row is None:
            raise NotFound("That change request no longer exists.")
        _live(db, row["item_id"])
        if row["status"] not in ("open", "in_progress"):
            raise ContentError("That change request is already closed.")
        if what is not None:
            if not what.strip():
                raise ContentError("Say what needs to change.")
            db.execute("UPDATE cm_change_requests SET what = ? WHERE id = ?", (what.strip()[:4000], request_id))
        if why is not None:
            db.execute("UPDATE cm_change_requests SET why = ? WHERE id = ?", (why.strip()[:4000], request_id))
        if assignee is not None and assignee != row["assignee"]:
            if assignee not in ("agent", "jarvis"):
                raise ContentError("Changes can go to the agent that made it, or to Jarvis.")
            db.execute("UPDATE cm_change_requests SET assignee = ?, status = 'open', picked_up_by = NULL, "
                       "picked_up_at = NULL, job_id = NULL, start_error = NULL WHERE id = ?",
                       (assignee, request_id))
            _event(db, row["item_id"], YOU, "reassigned",
                   "Sent to Jarvis" if assignee == "jarvis" else "Sent back to the agent that made it")
        else:
            _event(db, row["item_id"], YOU, "request_edited", "Edited the change request")
        _touch(db, row["item_id"])
        return row["item_id"]

    item_id = _tx(work)
    _announce(item_id, event_bus)
    return request_dict(get_db().execute("SELECT * FROM cm_change_requests WHERE id = ?",
                                         (request_id,)).fetchone())


def cancel_request(request_id: str, *, event_bus: Any = None) -> dict[str, Any]:
    """Withdraw a change request: the item goes back to Review as it was."""
    def work(db):
        row = db.execute("SELECT * FROM cm_change_requests WHERE id = ?", (request_id,)).fetchone()
        if row is None:
            raise NotFound("That change request no longer exists.")
        _live(db, row["item_id"])
        if row["status"] not in ("open", "in_progress"):
            raise ContentError("That change request is already closed.")
        stamp = now_iso()
        db.execute("UPDATE cm_change_requests SET status = 'cancelled', resolved_at = ? WHERE id = ?",
                   (stamp, request_id))
        db.execute("UPDATE cm_items SET stage = 'review', updated_at = ? WHERE id = ? AND stage = "
                   "'changes_requested'", (stamp, row["item_id"]))
        _event(db, row["item_id"], YOU, "request_withdrawn", "Withdrew the change request")
        return row["item_id"]

    return _announce(_tx(work), event_bus)


def record_job(request_id: str, *, starting: bool = False, job_id: str | None = None,
               error: str | None = None, event_bus: Any = None) -> None:
    """Where Jarvis's own revision job got to, in three separate steps.

    `starting` claims the request for Jarvis BEFORE the job exists; `job_id` then
    only attaches the job's id. Never the other way round: the job can finish —
    and resolve this very request — before the call that started it returns, and a
    status written after that point re-opened a request that was already answered
    (caught by forcing that race in a test). `error` gives it back, open, with the
    reason it could not start.
    """
    def work(db):
        row = db.execute("SELECT * FROM cm_change_requests WHERE id = ?", (request_id,)).fetchone()
        if row is None:
            return None
        if starting:
            db.execute("UPDATE cm_change_requests SET status = 'in_progress', picked_up_by = 'Jarvis', "
                       "picked_up_at = ?, start_error = NULL WHERE id = ? AND status IN ('open','in_progress')",
                       (now_iso(), request_id))
            _event(db, row["item_id"], "jarvis", "revision_started", "Jarvis started a background job to revise it")
        elif job_id:
            db.execute("UPDATE cm_change_requests SET job_id = ? WHERE id = ?", (job_id, request_id))
        else:
            db.execute("UPDATE cm_change_requests SET start_error = ?, status = 'open', picked_up_by = NULL, "
                       "picked_up_at = NULL WHERE id = ? AND status IN ('open','in_progress')",
                       ((error or "It couldn't start.")[:500], request_id))
            _event(db, row["item_id"], "jarvis", "revision_not_started", error or "It couldn't start.")
        return row["item_id"]

    item_id = _tx(work)
    if item_id:
        _announce(item_id, event_bus)


# --- where it goes (placements) -------------------------------------------------------------

def add_placement(item_id: str, *, platform: str, destination: str = "", by: str = YOU,
                  event_bus: Any = None) -> dict[str, Any]:
    def work(db):
        row = _live(db, item_id)
        if row["stage"] in ("archived", "changes_requested"):
            raise ContentError("Platforms can be added in Review, Ready to Post, Scheduling or Published.")
        _clean_platform(row["content_type"], platform)
        dest = (destination or "").strip()[:120]
        clash = db.execute("SELECT 1 FROM cm_placements WHERE item_id = ? AND platform = ? AND destination = ?",
                           (item_id, platform, dest)).fetchone()
        if clash:
            raise ContentError(f"It's already going to {platform_label(platform)}{' · ' + dest if dest else ''}.")
        placement_id = _id("cp")
        stamp = now_iso()
        db.execute("INSERT INTO cm_placements (id, item_id, platform, destination, created_at, updated_at) "
                   "VALUES (?,?,?,?,?,?)", (placement_id, item_id, platform, dest, stamp, stamp))
        _event(db, item_id, by, "platform_added", f"{platform_label(platform)}{' · ' + dest if dest else ''}")
        _touch(db, item_id)
        return placement_id

    placement_id = _tx(work)
    item = _announce(item_id, event_bus)
    return next(p for p in item["placements"] if p["id"] == placement_id)


def update_placement(placement_id: str, *, overrides: Any = None, media: Any = None,
                     destination: str | None = None, event_bus: Any = None) -> dict[str, Any]:
    """This platform's own version: its text (`overrides`, blank = use the
    item's), its own files (`media`, the full list for this platform — an empty
    list goes back to the shared files), and where on the platform it goes."""
    def work(db):
        p = _placement(db, placement_id)
        row = _live(db, p["item_id"])
        if p["status"] in ("queued", "publishing", "published"):
            raise ContentError("That post is already on its way or out — it can't be changed now.")
        if overrides is not None:
            from .kinds import platform_fields
            allowed = platform_fields(row["content_type"], p["platform"])
            clean = _clean_fields(row["content_type"], overrides)
            extra = [k for k in clean if k not in allowed]
            if extra:
                raise ContentError(f"{platform_label(p['platform'])} doesn't use {', '.join(extra)}.")
            current = store_loads(p["overrides_json"], {})
            merged = {k: v for k, v in {**current, **clean}.items() if v not in ("", [], None)}
            db.execute("UPDATE cm_placements SET overrides_json = ? WHERE id = ?", (dumps(merged), placement_id))
        if media is not None:
            own = _clean_media(row["content_type"], media, item_id=p["item_id"])
            files.claim_for_item([m["fileId"] for m in own], p["item_id"])
            db.execute("UPDATE cm_placements SET media_json = ? WHERE id = ?", (dumps(own), placement_id))
        if destination is not None:
            db.execute("UPDATE cm_placements SET destination = ? WHERE id = ?",
                       (destination.strip()[:120], placement_id))
        db.execute("UPDATE cm_placements SET updated_at = ? WHERE id = ?", (now_iso(), placement_id))
        what = "files" if media is not None and overrides is None else "version"
        _event(db, p["item_id"], YOU, "version_edited", f"Edited the {platform_label(p['platform'])} {what}")
        _touch(db, p["item_id"])
        return p["item_id"]

    item = _announce(_tx(work), event_bus)
    return next(p for p in item["placements"] if p["id"] == placement_id)


def remove_placement(placement_id: str, *, event_bus: Any = None) -> dict[str, Any]:
    def work(db):
        p = _placement(db, placement_id)
        _live(db, p["item_id"])
        if p["status"] not in ("draft", "failed"):
            raise ContentError("Cancel its schedule first — only an unscheduled platform can be removed.")
        db.execute("DELETE FROM cm_placements WHERE id = ?", (placement_id,))
        _event(db, p["item_id"], YOU, "platform_removed", f"Removed {platform_label(p['platform'])}")
        _derive(db, p["item_id"])
        _touch(db, p["item_id"])
        return p["item_id"]

    return _announce(_tx(work), event_bus)


def _approved_placement(db, placement_id: str):
    p = _placement(db, placement_id)
    row = _live(db, p["item_id"])
    if row["stage"] not in POST_APPROVAL:
        raise ContentError("It has to be approved before it can be posted or scheduled.")
    return p, row


def schedule(placement_id: str, *, scheduled_at: Any, timezone_name: str = "", by: str = YOU,
             event_bus: Any = None) -> dict[str, Any]:
    when = normalize_time(scheduled_at)
    if when is None:
        raise ContentError("Pick a date and time for it to go out.")
    if when <= now_iso():
        raise ContentError("That time has already passed — pick a time in the future, or use Post now.")

    def work(db):
        p, _row_ = _approved_placement(db, placement_id)
        if p["status"] not in ("draft", "failed", "scheduled"):
            raise ContentError("That post is already on its way or out.")
        kind = "rescheduled" if p["status"] == "scheduled" else "scheduled"
        db.execute("UPDATE cm_placements SET status = 'scheduled', scheduled_at = ?, timezone = ?, failure = NULL, "
                   "updated_at = ? WHERE id = ?", (when, (timezone_name or "")[:64], now_iso(), placement_id))
        _event(db, p["item_id"], by, kind, f"{platform_label(p['platform'])} · {when} ({timezone_name or 'UTC'})")
        _derive(db, p["item_id"])
        _touch(db, p["item_id"])
        return p["item_id"]

    item = _announce(_tx(work), event_bus)
    return next(x for x in item["placements"] if x["id"] == placement_id)


def unschedule(placement_id: str, *, event_bus: Any = None) -> dict[str, Any]:
    def work(db):
        p, _row_ = _approved_placement(db, placement_id)
        if p["status"] not in ("scheduled", "queued"):
            raise ContentError("That post isn't scheduled.")
        db.execute("UPDATE cm_placements SET status = 'draft', scheduled_at = NULL, updated_at = ? WHERE id = ?",
                   (now_iso(), placement_id))
        _event(db, p["item_id"], YOU, "unscheduled", f"{platform_label(p['platform'])} schedule cancelled")
        _derive(db, p["item_id"])
        _touch(db, p["item_id"])
        return p["item_id"]

    return _announce(_tx(work), event_bus)


def post_now(placement_id: str, *, event_bus: Any = None) -> dict[str, Any]:
    """Hand it to the publisher now: it goes to the front of the publish queue."""
    def work(db):
        p, _row_ = _approved_placement(db, placement_id)
        if p["status"] not in ("draft", "failed", "scheduled"):
            raise ContentError("That post is already on its way or out.")
        stamp = now_iso()
        db.execute("UPDATE cm_placements SET status = 'queued', scheduled_at = ?, failure = NULL, "
                   "claimed_by = NULL, claimed_at = NULL, updated_at = ? WHERE id = ?", (stamp, stamp, placement_id))
        _event(db, p["item_id"], YOU, "queued", f"{platform_label(p['platform'])} · post now")
        _derive(db, p["item_id"])
        _touch(db, p["item_id"])
        return p["item_id"]

    return _announce(_tx(work), event_bus)


def requeue(placement_id: str, *, event_bus: Any = None) -> dict[str, Any]:
    """Back in the queue: a publisher that claimed it went quiet, or it failed."""
    def work(db):
        p, _row_ = _approved_placement(db, placement_id)
        if p["status"] not in ("publishing", "failed"):
            raise ContentError("Only a failed post, or one a publisher went quiet on, can be put back in the queue.")
        db.execute("UPDATE cm_placements SET status = 'queued', claimed_by = NULL, claimed_at = NULL, failure = NULL, "
                   "updated_at = ? WHERE id = ?", (now_iso(), placement_id))
        _event(db, p["item_id"], YOU, "queued", f"{platform_label(p['platform'])} · back in the queue")
        _derive(db, p["item_id"])
        _touch(db, p["item_id"])
        return p["item_id"]

    return _announce(_tx(work), event_bus)


def mark_posted(placement_id: str, *, url: str = "", by: str = YOU, event_bus: Any = None) -> dict[str, Any]:
    """"I posted it myself" — recorded as published, with its link."""
    def work(db):
        p, _row_ = _approved_placement(db, placement_id)
        if p["status"] == "published":
            raise ContentError("That one is already marked as published.")
        stamp = now_iso()
        db.execute("UPDATE cm_placements SET status = 'published', published_at = ?, published_url = ?, "
                   "failure = NULL, updated_at = ? WHERE id = ?", (stamp, (url or "").strip()[:2000] or None,
                                                                   stamp, placement_id))
        _event(db, p["item_id"], by, "published", f"{platform_label(p['platform'])}{' · ' + url if url else ''}")
        _derive(db, p["item_id"])
        _touch(db, p["item_id"])
        return p["item_id"]

    return _announce(_tx(work), event_bus)


# --- publishing (the publisher) -------------------------------------------------------------

def claim(placement_id: str, *, by: str, event_bus: Any = None) -> dict[str, Any]:
    """A publisher taking one post off the queue. Exactly one claimant wins."""
    by = (by or "").strip()[:80] or "publisher"

    def work(db):
        p = _placement(db, placement_id)
        row = _live(db, p["item_id"])
        if row["stage"] == "archived":
            raise ContentError("That item is archived — it isn't going out.")
        stamp = now_iso()
        cursor = db.execute(
            "UPDATE cm_placements SET status = 'publishing', claimed_by = ?, claimed_at = ?, updated_at = ? "
            "WHERE id = ? AND (status = 'queued' OR (status = 'scheduled' AND scheduled_at <= ?))",
            (by, stamp, stamp, placement_id, stamp))
        if cursor.rowcount != 1:
            raise ContentError("That post isn't waiting to be published — someone else may have taken it.")
        _event(db, p["item_id"], by, "publishing", f"Posting to {platform_label(p['platform'])}")
        _derive(db, p["item_id"])
        _touch(db, p["item_id"])
        return p["item_id"]

    item = _announce(_tx(work), event_bus)
    return next(x for x in item["placements"] if x["id"] == placement_id)


def report_result(placement_id: str, *, ok: bool, url: str = "", error: str = "", by: str = "",
                  event_bus: Any = None) -> dict[str, Any]:
    by = (by or "").strip()[:80] or "publisher"

    def work(db):
        p = _placement(db, placement_id)
        row = _row(db, p["item_id"])
        if p["status"] not in ("publishing", "queued"):
            raise ContentError("That post wasn't being published, so there is no result to record.")
        stamp = now_iso()
        if ok:
            db.execute("UPDATE cm_placements SET status = 'published', published_at = ?, published_url = ?, "
                       "failure = NULL, updated_at = ? WHERE id = ?",
                       (stamp, (url or "").strip()[:2000] or None, stamp, placement_id))
            _event(db, p["item_id"], by, "published", f"{platform_label(p['platform'])}{' · ' + url if url else ''}")
        else:
            reason = (error or "The publisher didn't say why.").strip()[:1000]
            db.execute("UPDATE cm_placements SET status = 'failed', failure = ?, claimed_by = NULL, "
                       "claimed_at = NULL, updated_at = ? WHERE id = ?", (reason, stamp, placement_id))
            _event(db, p["item_id"], by, "publish_failed", f"{platform_label(p['platform'])}: {reason}")
        _derive(db, p["item_id"])
        _touch(db, p["item_id"])
        return p["item_id"], row["name"], platform_label(p["platform"])

    item_id, name, platform = _tx(work)
    if not ok:
        _notify(f"Post failed: {name}", f"{platform}: {(error or 'no reason given').strip()}", item_id,
                level="warn", event_bus=event_bus)
    item = _announce(item_id, event_bus)
    return next(x for x in item["placements"] if x["id"] == placement_id)


# --- reported numbers ---------------------------------------------------------------------------

_METRIC_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_ ]{0,39}$")


def record_metrics(placement_id: str, *, metrics: Any, captured_at: Any = None, by: str = "",
                   event_bus: Any = None) -> dict[str, Any]:
    """Numbers someone REPORTED for a published post — a publishing tool, an
    agent, Jarvis, or the person typing them in. Stored as a dated snapshot, so
    day 1 and day 30 both survive; nothing here is ever calculated."""
    if not isinstance(metrics, dict) or not metrics:
        raise ContentError('Send the numbers as an object, e.g. {"views": 1200, "likes": 85}.')
    values: dict[str, float] = {}
    for key, value in metrics.items():
        name = str(key).strip()
        if not _METRIC_NAME.match(name):
            raise ContentError(f"“{name}” isn't a name a number can be kept under.")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value < 0:
            raise ContentError(f"“{name}” must be a number of zero or more.")
        values[name] = int(value) if float(value).is_integer() else float(value)
    when = normalize_time(captured_at) if captured_at else now_iso()
    if when is None:
        raise ContentError("“capturedAt” must be a date and time with its timezone.")
    source = (by or "").strip()[:80] or YOU

    def work(db):
        p = _placement(db, placement_id)
        _row(db, p["item_id"])
        if p["status"] != "published":
            raise ContentError("Numbers can only be recorded for a post that has been published.")
        db.execute("INSERT INTO cm_metrics (placement_id, item_id, captured_at, reported_at, source, metrics_json) "
                   "VALUES (?,?,?,?,?,?)", (placement_id, p["item_id"], when, now_iso(), source, dumps(values)))
        latest = db.execute("SELECT * FROM cm_metrics WHERE placement_id = ? ORDER BY captured_at DESC, id DESC "
                            "LIMIT 1", (placement_id,)).fetchone()
        db.execute("UPDATE cm_placements SET metrics_json = ? WHERE id = ?",
                   (dumps({"capturedAt": latest["captured_at"], "source": latest["source"],
                           "values": store_loads(latest["metrics_json"], {})}), placement_id))
        return p["item_id"]

    item = _announce(_tx(work), event_bus)
    return next(x for x in item["placements"] if x["id"] == placement_id)


# --- archive and recycle bin -------------------------------------------------------------------

def archive(item_id: str, *, event_bus: Any = None) -> dict[str, Any]:
    def work(db):
        row = _live(db, item_id)
        if row["stage"] == "archived":
            raise ContentError("It's already archived.")
        _cancel_pending(db, item_id, "archived")
        stamp = now_iso()
        db.execute("UPDATE cm_items SET archived_from = ?, stage = 'archived', archived_at = ?, updated_at = ? "
                   "WHERE id = ?", (row["stage"], stamp, stamp, item_id))
        _event(db, item_id, YOU, "archived", "Archived")

    _tx(work)
    return _announce(item_id, event_bus)


def unarchive(item_id: str, *, event_bus: Any = None) -> dict[str, Any]:
    def work(db):
        row = _live(db, item_id)
        if row["stage"] != "archived":
            raise ContentError("It isn't archived.")
        back = row["archived_from"] or "review"
        db.execute("UPDATE cm_items SET stage = ?, archived_at = NULL, archived_from = NULL, updated_at = ? "
                   "WHERE id = ?", (back, now_iso(), item_id))
        _event(db, item_id, YOU, "unarchived", "Brought back from the archive")
        _derive(db, item_id)

    _tx(work)
    return _announce(item_id, event_bus)


def delete(item_id: str, *, event_bus: Any = None) -> dict[str, Any]:
    """To the recycle bin. Scheduled posts are cancelled — a schedule that came due
    while it sat in the bin would otherwise go out the moment it was restored."""
    def work(db):
        _live(db, item_id)
        _cancel_pending(db, item_id, "moved to the Recycle Bin")
        _derive(db, item_id)
        stamp = now_iso()
        db.execute("UPDATE cm_items SET deleted_at = ?, updated_at = ? WHERE id = ?", (stamp, stamp, item_id))
        _event(db, item_id, YOU, "deleted", "Moved to the Recycle Bin")

    _tx(work)
    return _announce(item_id, event_bus)


def restore(item_id: str, *, event_bus: Any = None) -> dict[str, Any]:
    def work(db):
        row = _row(db, item_id)
        if not row["deleted_at"]:
            raise ContentError("It isn't in the Recycle Bin.")
        db.execute("UPDATE cm_items SET deleted_at = NULL, updated_at = ? WHERE id = ?", (now_iso(), item_id))
        _event(db, item_id, YOU, "restored", "Restored from the Recycle Bin")

    _tx(work)
    return _announce(item_id, event_bus)


def purge(item_id: str, *, event_bus: Any = None) -> None:
    """Delete forever: every row and every file. Only from the recycle bin."""
    with _lock:
        row = _row(get_db(), item_id)
        if not row["deleted_at"]:
            raise ContentError("Only something in the Recycle Bin can be deleted forever.")
        paths = files.paths_for_item(item_id)
        _tx(lambda db: db.execute("DELETE FROM cm_items WHERE id = ?", (item_id,)))
        for path in paths:
            path.unlink(missing_ok=True)
    (event_bus or default_bus).publish(EventType.CONTENT_CHANGED, {"id": item_id, "purged": True})


def empty_bin(*, event_bus: Any = None) -> int:
    ids = [r["id"] for r in get_db().execute("SELECT id FROM cm_items WHERE deleted_at IS NOT NULL")]
    for item_id in ids:
        purge(item_id, event_bus=event_bus)
    return len(ids)
