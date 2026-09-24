"""Reading content items: one item, a filtered list, the stage counts.

Also the one place that answers "what happens next, and who does it" for an
item (`_next_step`) and "does this need the person" (`_attention`) — computed
here, on the server, so the screen, the agent API and Jarvis's own
`content_status` tool can never disagree about it.

Writes live in `lifecycle.py`. A leaf apart from the database.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from ..db import get_db
from ..jscompat import now_iso, to_iso_z
from . import files
from .kinds import STAGE_IDS, platform_label, type_label

PENDING = ("scheduled", "queued", "publishing")
#: A publisher that claimed a post and has said nothing for this long has
#: probably died; the person is offered "put it back in the queue".
STALE_CLAIM = timedelta(minutes=30)


def _loads(text: Any, fallback: Any) -> Any:
    try:
        value = json.loads(text) if text else fallback
    except (TypeError, ValueError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None


def normalize_time(value: Any) -> str | None:
    """Any ISO instant WITH an offset, in the stored UTC form. A naive time is
    refused rather than guessed: which timezone it meant is exactly the thing a
    schedule cannot get wrong."""
    moment = _parse(str(value)) if value else None
    if moment is None or moment.tzinfo is None:
        return None
    return to_iso_z(moment)


# --- rows -> dicts -------------------------------------------------------------

def _media(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    known = files.lookup([str(e.get("fileId")) for e in entries if e.get("fileId")])
    out = []
    for entry in entries:
        info = known.get(str(entry.get("fileId")))
        if info is None:
            continue
        out.append({**info, "role": entry.get("role") or "attachment",
                    "order": int(entry.get("order") or 0)})
    out.sort(key=lambda m: (m["role"], m["order"]))
    return out


def placement_dict(row: Any, now: str | None = None) -> dict[str, Any]:
    now = now or now_iso()
    status = row["status"]
    claimed = _parse(row["claimed_at"])
    return {
        "id": row["id"], "itemId": row["item_id"], "platform": row["platform"],
        "platformLabel": platform_label(row["platform"]),
        "destination": row["destination"], "overrides": _loads(row["overrides_json"], {}),
        # This platform's OWN media only; a role it has none of uses the item's.
        "media": _media(_loads(row["media_json"], [])),
        "status": status, "scheduledAt": row["scheduled_at"], "timezone": row["timezone"],
        "due": status == "scheduled" and bool(row["scheduled_at"]) and row["scheduled_at"] <= now,
        "claimedBy": row["claimed_by"], "claimedAt": row["claimed_at"],
        "stale": status == "publishing" and claimed is not None
                 and datetime.now(timezone.utc) - claimed > STALE_CLAIM,
        "publishedAt": row["published_at"], "publishedUrl": row["published_url"],
        "failure": row["failure"], "metrics": _loads(row["metrics_json"], {}) or None,
        "createdAt": row["created_at"], "updatedAt": row["updated_at"],
    }


def request_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"], "itemId": row["item_id"], "revision": row["revision"],
        "what": row["what"], "why": row["why"], "assignee": row["assignee"],
        "status": row["status"], "pickedUpBy": row["picked_up_by"],
        "pickedUpAt": row["picked_up_at"], "jobId": row["job_id"],
        "startError": row["start_error"], "createdAt": row["created_at"],
        "resolvedAt": row["resolved_at"], "resolvedRevision": row["resolved_revision"],
    }


def _open_request(item_id: str) -> dict[str, Any] | None:
    row = get_db().execute(
        "SELECT * FROM cm_change_requests WHERE item_id = ? AND status IN ('open','in_progress') "
        "ORDER BY created_at DESC LIMIT 1", (item_id,)).fetchone()
    return request_dict(row) if row else None


def placements_for(item_id: str) -> list[dict[str, Any]]:
    now = now_iso()
    rows = get_db().execute("SELECT * FROM cm_placements WHERE item_id = ? ORDER BY created_at",
                            (item_id,)).fetchall()
    return [placement_dict(r, now) for r in rows]


def _item(row: Any) -> dict[str, Any]:
    item = {
        "id": row["id"], "name": row["name"], "contentType": row["content_type"],
        "typeLabel": type_label(row["content_type"]), "niche": row["niche"],
        "stage": row["stage"], "producer": row["producer"], "revision": row["revision"],
        "fields": _loads(row["fields_json"], {}),
        "media": _media(_loads(row["media_json"], [])),
        "findings": _loads(row["findings_json"], []),
        "approvedAt": row["approved_at"], "archivedAt": row["archived_at"],
        "archivedFrom": row["archived_from"], "deletedAt": row["deleted_at"],
        "createdAt": row["created_at"], "updatedAt": row["updated_at"],
    }
    item["placements"] = placements_for(row["id"])
    item["openRequest"] = _open_request(row["id"])
    item["editable"] = editable(item["stage"], [p["status"] for p in item["placements"]],
                                bool(item["deletedAt"]))
    item["attention"] = _attention(item)
    item["next"] = _next_step(item)
    return item


def editable(stage: str, statuses: list[str], deleted: bool = False) -> bool:
    """Whether the content (its text and files) can still be changed.

    From Review until it has gone out: a scheduled post is still just a plan, so
    it stays editable, and the change goes out with it. Not while a post is on its
    way (queued or being posted — the publisher may already hold the old version),
    not once every platform has published, and not while changes are being made
    by someone else or it is archived or in the bin.
    """
    if deleted or stage not in ("review", "approved", "scheduling", "published"):
        return False
    if any(s in ("queued", "publishing") for s in statuses):
        return False
    return not (statuses and all(s == "published" for s in statuses))


def _who_revises(item: dict[str, Any], request: dict[str, Any]) -> str:
    if request.get("pickedUpBy"):
        return request["pickedUpBy"]
    if request.get("assignee") == "jarvis":
        return "Jarvis"
    return item.get("producer") or "The agent that made it"


def _attention(item: dict[str, Any]) -> list[str]:
    """What needs the PERSON, in their words. Empty when nothing does."""
    if item["deletedAt"]:
        return []
    notes: list[str] = []
    if item["stage"] == "review":
        notes.append("Revision ready to review" if item["revision"] > 1 else "Ready for your review")
    for p in item["placements"]:
        if p["status"] == "failed":
            notes.append(f"Post failed on {p['platformLabel']}")
        elif p["stale"]:
            notes.append(f"{p['platformLabel']} post has gone quiet")
    request = item["openRequest"]
    if item["stage"] == "changes_requested" and request and request.get("startError"):
        notes.append("The revision couldn't start")
    return notes


def _next_step(item: dict[str, Any]) -> dict[str, str]:
    """{step, who}: the very next thing that has to happen to this item, and
    who or what is responsible for it."""
    if item["deletedAt"]:
        return {"step": "Restore it, or delete it forever", "who": "You"}
    stage = item["stage"]
    placements = item["placements"]
    if stage == "review":
        return {"step": "Review it: approve, or ask for changes", "who": "You"}
    if stage == "changes_requested":
        request = item["openRequest"] or {}
        who = _who_revises(item, request)
        if request.get("startError"):
            return {"step": "The revision couldn't start — try again or reassign it", "who": "You"}
        if request.get("status") == "in_progress" or request.get("jobId"):
            return {"step": "Working on the revision", "who": who}
        return {"step": "Pick up the change request and revise it", "who": who}
    if stage == "approved":
        if any(p["status"] == "failed" for p in placements):
            return {"step": "Retry or reschedule the failed post", "who": "You"}
        return {"step": "Choose where it goes, then post or schedule it", "who": "You"}
    if stage == "scheduling":
        pending = [p for p in placements if p["status"] in PENDING]
        if any(p["status"] == "publishing" for p in pending):
            p = next(p for p in pending if p["status"] == "publishing")
            return {"step": f"Being posted to {p['platformLabel']}", "who": p["claimedBy"] or "Publisher"}
        if any(p["status"] == "queued" or p["due"] for p in pending):
            p = next(p for p in pending if p["status"] == "queued" or p["due"])
            return {"step": f"Waiting for the publisher to post it to {p['platformLabel']}",
                    "who": "Publisher"}
        soonest = min(pending, key=lambda p: p["scheduledAt"] or "")
        return {"step": f"Goes out on {soonest['platformLabel']} as scheduled",
                "who": "Publisher"}
    if stage == "published":
        if any(p["status"] == "failed" for p in placements):
            return {"step": "Retry the failed post, or leave it", "who": "You"}
        return {"step": "Nothing — it's live", "who": "—"}
    if stage == "archived":
        return {"step": "Nothing — it's archived", "who": "—"}
    return {"step": "", "who": ""}


# --- reading -------------------------------------------------------------------

def get_item(item_id: str) -> dict[str, Any] | None:
    row = get_db().execute("SELECT * FROM cm_items WHERE id = ?", (item_id,)).fetchone()
    return _item(row) if row else None


def get_row(item_id: str) -> Any:
    return get_db().execute("SELECT * FROM cm_items WHERE id = ?", (item_id,)).fetchone()


def item_detail(item_id: str) -> dict[str, Any] | None:
    item = get_item(item_id)
    if item is None:
        return None
    db = get_db()
    item["requests"] = [request_dict(r) for r in db.execute(
        "SELECT * FROM cm_change_requests WHERE item_id = ? ORDER BY created_at DESC", (item_id,))]
    item["revisions"] = [
        {"revision": r["revision"], "fields": _loads(r["fields_json"], {}),
         "media": _media(_loads(r["media_json"], [])), "by": r["by"], "note": r["note"],
         "createdAt": r["created_at"]}
        for r in db.execute("SELECT * FROM cm_revisions WHERE item_id = ? ORDER BY revision DESC",
                            (item_id,))]
    item["events"] = [
        {"at": r["at"], "actor": r["actor"], "kind": r["kind"], "note": r["note"]}
        for r in db.execute("SELECT * FROM cm_events WHERE item_id = ? ORDER BY id DESC", (item_id,))]
    for placement in item["placements"]:
        if placement["status"] == "published":
            placement["metricsHistory"] = metrics_history(placement["id"])
    return item


def _filters(*, niche: str | None, content_type: str | None, platform: str | None,
             q: str | None, no_niche: bool = False) -> tuple[list[str], list[Any]]:
    where: list[str] = []
    args: list[Any] = []
    if no_niche:
        where.append("i.niche = ''")
    elif niche:
        where.append("i.niche = ? COLLATE NOCASE")
        args.append(niche)
    if content_type:
        where.append("i.content_type = ?")
        args.append(content_type)
    if platform:
        where.append("EXISTS (SELECT 1 FROM cm_placements p WHERE p.item_id = i.id AND p.platform = ?)")
        args.append(platform)
    if q and q.strip():
        like = f"%{q.strip()}%"
        # The field VALUES, never the stored JSON: a LIKE over `fields_json` also
        # matched the key names, so searching "title" found almost everything.
        where.append("(i.name LIKE ? OR i.niche LIKE ? OR i.producer LIKE ? OR EXISTS "
                     "(SELECT 1 FROM json_each(i.fields_json) f WHERE f.value LIKE ?))")
        args += [like, like, like, like]
    return where, args


#: Which items a post-approval view holds. An item is in EVERY stage one of its
#: platforms is in — YouTube scheduled and TikTok published puts it under both —
#: so nothing live on one platform is missing from Published while another waits.
_POST_APPROVAL = "i.stage IN ('approved','scheduling','published')"


def _has(statuses: str) -> str:
    return f"EXISTS (SELECT 1 FROM cm_placements p WHERE p.item_id = i.id AND p.status IN ({statuses}))"


_READY = _has("'draft','failed'")
_PENDING_SQL = _has("'scheduled','queued','publishing'")
_OUT = _has("'published'")
_FAILED = _has("'failed'")
_NO_PLATFORMS = "NOT EXISTS (SELECT 1 FROM cm_placements p WHERE p.item_id = i.id)"
_VIEW_SQL = {
    "approved": f"({_POST_APPROVAL} AND ({_NO_PLATFORMS} OR {_READY}))",
    "scheduling": f"({_POST_APPROVAL} AND {_PENDING_SQL})",
    "published": f"({_POST_APPROVAL} AND {_OUT})",
}
#: The platform statuses each post-approval view is about.
VIEW_STATUSES = {"approved": ("draft", "failed"), "scheduling": PENDING, "published": ("published",)}


#: "All" inside a folder: everything still in play — every type, every stage
#: before the archive. Not a stage an item is in; a view over all of them.
ACTIVE = "active"


def _stage_clause(stage: str) -> tuple[str, list[Any]]:
    if stage == ACTIVE:
        return "i.stage != 'archived'", []
    if stage in _VIEW_SQL:
        return _VIEW_SQL[stage], []
    return "i.stage = ?", [stage]


#: Which date a "from/to" filter means depends on where you are looking.
_DATE_COLUMN = {
    "archived": "i.archived_at",
    "bin": "i.deleted_at",
    "published": "(SELECT MAX(p.published_at) FROM cm_placements p WHERE p.item_id = i.id)",
}


def _list_query(*, stage: str | None, niche: str | None, content_type: str | None, platform: str | None,
                q: str | None, date_from: str | None, date_to: str | None,
                archived_from: str | None, no_niche: bool = False) -> tuple[str, list[Any], str]:
    where, args = _filters(niche=niche, content_type=content_type, platform=platform, q=q, no_niche=no_niche)
    if stage == "bin":
        where.append("i.deleted_at IS NOT NULL")
        order = "i.deleted_at DESC"
    else:
        where.append("i.deleted_at IS NULL")
        if stage:
            clause, extra = _stage_clause(stage)
            where.append(clause)
            args += extra
        # Review is a queue: oldest first. Everything else: most recent first.
        order = "i.updated_at ASC" if stage == "review" else "i.updated_at DESC"
    if archived_from:
        where.append("i.archived_from = ?")
        args.append(archived_from)
    column = _DATE_COLUMN.get(stage or "", "i.created_at")
    if date_from:
        where.append(f"{column} >= ?")
        args.append(date_from)
    if date_to:
        where.append(f"{column} <= ?")
        args.append(date_to)
    return " AND ".join(where), args, order


def list_items(*, stage: str | None = None, niche: str | None = None,
               content_type: str | None = None, platform: str | None = None,
               q: str | None = None, date_from: str | None = None,
               date_to: str | None = None, archived_from: str | None = None, no_niche: bool = False,
               limit: int | None = None, offset: int = 0) -> list[dict[str, Any]]:
    where, args, order = _list_query(stage=stage, niche=niche, content_type=content_type, platform=platform,
                                     q=q, date_from=date_from, date_to=date_to, archived_from=archived_from,
                                     no_niche=no_niche)
    sql = f"SELECT i.* FROM cm_items i WHERE {where} ORDER BY {order}, i.id"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        args = [*args, max(1, int(limit)), max(0, int(offset))]
    return [_item(row) for row in get_db().execute(sql, args).fetchall()]


def count_items(**filters: Any) -> int:
    where, args, _order = _list_query(**{"stage": None, "niche": None, "content_type": None, "platform": None,
                                          "q": None, "date_from": None, "date_to": None,
                                          "archived_from": None, **filters})
    return get_db().execute(f"SELECT COUNT(*) FROM cm_items i WHERE {where}", args).fetchone()[0]


def summary(*, niche: str | None = None, content_type: str | None = None,
            platform: str | None = None, q: str | None = None, no_niche: bool = False) -> dict[str, Any]:
    """Counts per stage (and the bin) under the same filters the list uses, plus
    what needs the person right now — in one pass over the items. An item counts
    in every post-approval stage one of its platforms is in, exactly as the lists
    show it; `posts` says how many platform posts each of those stages holds."""
    where, args = _filters(niche=niche, content_type=content_type, platform=platform, q=q, no_niche=no_niche)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    live = "i.deleted_at IS NULL"
    active = f"{live} AND i.stage != 'archived'"
    cases = {
        "bin": "i.deleted_at IS NOT NULL",
        "active": active,
        "review": f"{live} AND i.stage = 'review'",
        "changes_requested": f"{live} AND i.stage = 'changes_requested'",
        "archived": f"{live} AND i.stage = 'archived'",
        **{view: f"{live} AND {sql}" for view, sql in _VIEW_SQL.items()},
        "toReview": f"{live} AND i.stage = 'review' AND i.revision = 1",
        "revisionsReady": f"{live} AND i.stage = 'review' AND i.revision > 1",
        "failedPosts": f"{active} AND {_FAILED}",
        "revisionsStuck": (f"{live} AND i.stage = 'changes_requested' AND EXISTS (SELECT 1 FROM "
                           "cm_change_requests r WHERE r.item_id = i.id AND r.status IN ('open','in_progress') "
                           "AND r.start_error IS NOT NULL)"),
    }
    select = ", ".join(f"COALESCE(SUM(CASE WHEN {cond} THEN 1 ELSE 0 END), 0) AS {name}"
                       for name, cond in cases.items())
    db = get_db()
    row = db.execute(f"SELECT {select} FROM cm_items i {clause}", args).fetchone()
    counts = {stage: row[stage] for stage in STAGE_IDS}
    counts["bin"] = row["bin"]
    counts[ACTIVE] = row["active"]

    post_where, post_args = _filters(niche=niche, content_type=content_type, platform=None, q=q,
                                     no_niche=no_niche)
    post_where += [live, _POST_APPROVAL]
    if platform:
        post_where.append("p.platform = ?")
        post_args.append(platform)
    posts = {view: 0 for view in VIEW_STATUSES}
    for r in db.execute(f"SELECT p.status AS status, COUNT(*) AS n FROM cm_placements p JOIN cm_items i "
                        f"ON i.id = p.item_id WHERE {' AND '.join(post_where)} GROUP BY p.status", post_args):
        for view, statuses in VIEW_STATUSES.items():
            if r["status"] in statuses:
                posts[view] += r["n"]
    return {"counts": counts, "posts": posts,
            "attention": {key: row[key] for key in ("toReview", "revisionsReady", "failedPosts",
                                                     "revisionsStuck")}}


def niches() -> list[str]:
    """Every niche folder, empty ones included."""
    rows = get_db().execute("SELECT name FROM cm_niches").fetchall()
    return sorted((r[0] for r in rows), key=str.lower)


def _folder(name: str | None = None, created_at: str | None = None) -> dict[str, Any]:
    folder: dict[str, Any] = {"total": 0, "byType": {}, "review": 0, "archived": 0, "binned": 0,
                              "updatedAt": None}
    if name is not None:
        folder.update({"name": name, "createdAt": created_at})
    return folder


def niche_overview() -> dict[str, Any]:
    """The folders, for the first screen — in one grouped pass. Each niche (empty
    ones too), the items with no niche, and everything together: what is still in
    play (not archived, not in the bin) by type, how many wait in Review, and what
    sits in the archive or the bin — the reason a niche with nothing in play
    still cannot be deleted."""
    db = get_db()
    folders = {r["name"].lower(): _folder(r["name"], r["created_at"])
               for r in db.execute("SELECT name, created_at FROM cm_niches")}
    none, everything = _folder(), _folder()
    rows = db.execute(
        "SELECT niche, content_type, "
        "SUM(deleted_at IS NULL AND stage != 'archived') AS active, "
        "SUM(deleted_at IS NULL AND stage = 'review') AS review, "
        "SUM(deleted_at IS NULL AND stage = 'archived') AS archived, "
        "SUM(deleted_at IS NOT NULL) AS binned, "
        "MAX(CASE WHEN deleted_at IS NULL THEN updated_at END) AS updated "
        "FROM cm_items GROUP BY niche, content_type").fetchall()
    for r in rows:
        name = r["niche"] or ""
        # Every write goes through a folder; a stray label still shows as one.
        target = folders.setdefault(name.lower(), _folder(name)) if name else none
        for folder in (target, everything):
            if r["active"]:
                folder["total"] += r["active"]
                folder["byType"][r["content_type"]] = folder["byType"].get(r["content_type"], 0) + r["active"]
            folder["review"] += r["review"]
            folder["archived"] += r["archived"]
            folder["binned"] += r["binned"]
            if r["updated"] and (folder["updatedAt"] is None or r["updated"] > folder["updatedAt"]):
                folder["updatedAt"] = r["updated"]
    return {"niches": sorted(folders.values(), key=lambda f: f["name"].lower()), "none": none,
            "all": everything}


def calendar(*, start: str, end: str, niche: str | None = None, content_type: str | None = None,
             platform: str | None = None, q: str | None = None, no_niche: bool = False) -> list[dict[str, Any]]:
    """Every placement with a date in [start, end): scheduled/queued ones by
    their schedule, published ones by when they went out."""
    where, args = _filters(niche=niche, content_type=content_type, platform=None, q=q, no_niche=no_niche)
    where.append("i.deleted_at IS NULL AND i.stage != 'archived'")
    if platform:
        where.append("p.platform = ?")
        args.append(platform)
    sql = (f"SELECT p.*, i.name AS item_name, i.content_type AS item_type, i.niche AS item_niche "
           f"FROM cm_placements p JOIN cm_items i ON i.id = p.item_id WHERE {' AND '.join(where)} "
           f"AND COALESCE(p.published_at, p.scheduled_at) >= ? AND COALESCE(p.published_at, p.scheduled_at) < ? "
           f"AND p.status != 'draft' ORDER BY COALESCE(p.published_at, p.scheduled_at)")
    now = now_iso()
    out = []
    for row in get_db().execute(sql, [*args, start, end]).fetchall():
        entry = placement_dict(row, now)
        entry.update({"itemName": row["item_name"], "contentType": row["item_type"],
                      "typeLabel": type_label(row["item_type"]), "niche": row["item_niche"]})
        out.append(entry)
    return out


# --- for agents ------------------------------------------------------------------

def merged_version(item: dict[str, Any], placement: dict[str, Any]) -> dict[str, Any]:
    """What actually gets posted to one platform: the base supporting fields, with
    this platform's own overrides on top, limited to the fields it uses."""
    from .kinds import platform_fields
    wanted = platform_fields(item["contentType"], placement["platform"])
    base = item["fields"]
    over = placement["overrides"]
    merged = {f: (over[f] if over.get(f) not in (None, "", []) else base.get(f)) for f in wanted}
    # A field nobody filled in is left out, not sent to a publisher as null.
    return {f: v for f, v in merged.items() if v not in (None, "", [])}


def merged_media(item: dict[str, Any], placement: dict[str, Any]) -> list[dict[str, Any]]:
    """The files that actually go to one platform. Shared by default: for each
    role (the video, the slides, the thumbnail, the cover…) the platform uses its
    OWN files when it has any of that role, and the item's otherwise. So "same
    video, own cover" is a platform holding just a cover."""
    own = placement.get("media") or []
    own_roles = {m["role"] for m in own}
    merged = [m for m in item["media"] if m["role"] not in own_roles] + own
    merged.sort(key=lambda m: (m["role"], m["order"]))
    return merged


def publish_queue() -> list[dict[str, Any]]:
    """Everything a publisher should post now: queued, or scheduled and due.
    Never an archived or deleted item's."""
    now = now_iso()
    rows = get_db().execute(
        "SELECT p.* FROM cm_placements p JOIN cm_items i ON i.id = p.item_id "
        "WHERE i.deleted_at IS NULL AND i.stage != 'archived' AND "
        "(p.status = 'queued' OR (p.status = 'scheduled' AND p.scheduled_at <= ?)) "
        "ORDER BY COALESCE(p.scheduled_at, p.updated_at)", (now,)).fetchall()
    out = []
    for row in rows:
        placement = placement_dict(row, now)
        item = get_item(row["item_id"])
        if item is None:
            continue
        out.append({
            "placementId": placement["id"], "itemId": item["id"], "name": item["name"],
            "contentType": item["contentType"], "niche": item["niche"],
            "platform": placement["platform"],
            "destination": placement["destination"], "scheduledAt": placement["scheduledAt"],
            "timezone": placement["timezone"], "version": merged_version(item, placement),
            "media": merged_media(item, placement),
        })
    return out


def open_change_requests(status: str | None = None, assignee: str | None = None) -> list[dict[str, Any]]:
    """Change requests an agent can act on — never an archived or deleted item's."""
    statuses = [s for s in (status or "open,in_progress").split(",") if s]
    marks = ",".join("?" * len(statuses))
    args: list[Any] = [*statuses]
    sql = (f"SELECT r.* FROM cm_change_requests r JOIN cm_items i ON i.id = r.item_id "
           f"WHERE r.status IN ({marks}) AND i.deleted_at IS NULL AND i.stage = 'changes_requested'")
    if assignee:
        sql += " AND r.assignee = ?"
        args.append(assignee)
    out = []
    for row in get_db().execute(sql + " ORDER BY r.created_at", args).fetchall():
        request = request_dict(row)
        item = get_item(row["item_id"])
        if item:
            request["item"] = {k: item[k] for k in ("id", "name", "contentType", "niche", "producer",
                                                    "revision", "fields", "media")}
        out.append(request)
    return out


def utc_now_plus(minutes: int) -> str:
    return to_iso_z(datetime.now(timezone.utc) + timedelta(minutes=minutes))


# --- analytics -------------------------------------------------------------------

def metrics_history(placement_id: str) -> list[dict[str, Any]]:
    rows = get_db().execute("SELECT * FROM cm_metrics WHERE placement_id = ? ORDER BY captured_at DESC, id DESC",
                            (placement_id,)).fetchall()
    return [{"capturedAt": r["captured_at"], "reportedAt": r["reported_at"], "source": r["source"],
             "values": _loads(r["metrics_json"], {})} for r in rows]


def analytics(*, niche: str | None = None, content_type: str | None = None, platform: str | None = None,
              q: str | None = None, date_from: str | None = None, date_to: str | None = None,
              no_niche: bool = False) -> dict[str, Any]:
    """Every published post under the filters, with its latest REPORTED numbers,
    and totals of what was reported. A post nobody has reported numbers for is
    listed with none — never with zeros that look like a real result."""
    where, args = _filters(niche=niche, content_type=content_type, platform=None, q=q, no_niche=no_niche)
    where += ["i.deleted_at IS NULL", "p.status = 'published'"]
    if platform:
        where.append("p.platform = ?")
        args.append(platform)
    if date_from:
        where.append("p.published_at >= ?")
        args.append(date_from)
    if date_to:
        where.append("p.published_at <= ?")
        args.append(date_to)
    rows = get_db().execute(
        f"SELECT p.id, p.item_id, p.platform, p.destination, p.published_at, p.published_url, p.metrics_json, "
        f"i.name, i.content_type, i.niche FROM cm_placements p JOIN cm_items i ON i.id = p.item_id "
        f"WHERE {' AND '.join(where)} ORDER BY p.published_at DESC", args).fetchall()
    posts, totals, reported = [], {}, 0
    by_platform: dict[str, dict[str, Any]] = {}
    for r in rows:
        latest = _loads(r["metrics_json"], {}) or None
        values = (latest or {}).get("values") or {}
        if values:
            reported += 1
        group = by_platform.setdefault(r["platform"], {"platform": r["platform"],
                                                       "platformLabel": platform_label(r["platform"]),
                                                       "posts": 0, "reported": 0, "totals": {}})
        group["posts"] += 1
        group["reported"] += 1 if values else 0
        for key, value in values.items():
            totals[key] = totals.get(key, 0) + value
            group["totals"][key] = group["totals"].get(key, 0) + value
        posts.append({"placementId": r["id"], "itemId": r["item_id"], "name": r["name"],
                      "contentType": r["content_type"], "typeLabel": type_label(r["content_type"]),
                      "niche": r["niche"], "platform": r["platform"], "platformLabel": platform_label(r["platform"]),
                      "destination": r["destination"], "publishedAt": r["published_at"],
                      "publishedUrl": r["published_url"], "metrics": latest})
    return {"posts": posts, "totals": totals, "reported": reported,
            "byPlatform": sorted(by_platform.values(), key=lambda g: -g["posts"])}

