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
        "accountId": row["account_id"], "accountLabel": row["account_label"],
        "destination": row["destination"], "overrides": _loads(row["overrides_json"], {}),
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
    item["attention"] = _attention(item)
    item["next"] = _next_step(item)
    return item


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
        return {"step": f"Goes out on {soonest['platformLabel']} at the scheduled time",
                "who": "Publisher (at the scheduled time)"}
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
    return item


def _filters(*, niche: str | None, content_type: str | None, platform: str | None,
             q: str | None) -> tuple[list[str], list[Any]]:
    where: list[str] = []
    args: list[Any] = []
    if niche:
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
        where.append("(i.name LIKE ? OR i.niche LIKE ? OR i.fields_json LIKE ? OR i.producer LIKE ?)")
        args += [like, like, like, like]
    return where, args


#: Which date a "from/to" filter means depends on where you are looking.
_DATE_COLUMN = {
    "archived": "i.archived_at",
    "bin": "i.deleted_at",
    "published": "(SELECT MAX(p.published_at) FROM cm_placements p WHERE p.item_id = i.id)",
}


def list_items(*, stage: str | None = None, niche: str | None = None,
               content_type: str | None = None, platform: str | None = None,
               q: str | None = None, date_from: str | None = None,
               date_to: str | None = None, archived_from: str | None = None) -> list[dict[str, Any]]:
    where, args = _filters(niche=niche, content_type=content_type, platform=platform, q=q)
    if stage == "bin":
        where.append("i.deleted_at IS NOT NULL")
        order = "i.deleted_at DESC"
    else:
        where.append("i.deleted_at IS NULL")
        if stage:
            where.append("i.stage = ?")
            args.append(stage)
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
    sql = f"SELECT i.* FROM cm_items i WHERE {' AND '.join(where)} ORDER BY {order}"
    return [_item(row) for row in get_db().execute(sql, args).fetchall()]


def summary(*, niche: str | None = None, content_type: str | None = None,
            platform: str | None = None, q: str | None = None) -> dict[str, Any]:
    """Counts per stage (and the bin) under the same filters the list uses, plus
    what needs the person right now."""
    where, args = _filters(niche=niche, content_type=content_type, platform=platform, q=q)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    db = get_db()
    counts = {stage: 0 for stage in STAGE_IDS}
    counts["bin"] = 0
    for row in db.execute(
            f"SELECT CASE WHEN i.deleted_at IS NOT NULL THEN 'bin' ELSE i.stage END AS s, "
            f"COUNT(*) AS n FROM cm_items i {clause} GROUP BY s", args):
        counts[row["s"]] = row["n"]

    active = f"{clause + ' AND' if clause else 'WHERE'} i.deleted_at IS NULL AND i.stage != 'archived'"
    to_review = db.execute(f"SELECT COUNT(*) AS n FROM cm_items i {active} AND i.stage = 'review' "
                           "AND i.revision = 1", args).fetchone()["n"]
    revisions = db.execute(f"SELECT COUNT(*) AS n FROM cm_items i {active} AND i.stage = 'review' "
                           "AND i.revision > 1", args).fetchone()["n"]
    failed = db.execute(f"SELECT COUNT(*) AS n FROM cm_items i {active} AND EXISTS (SELECT 1 FROM "
                        "cm_placements p WHERE p.item_id = i.id AND p.status = 'failed')", args).fetchone()["n"]
    stuck = db.execute(f"SELECT COUNT(*) AS n FROM cm_items i {active} AND i.stage = 'changes_requested' "
                       "AND EXISTS (SELECT 1 FROM cm_change_requests r WHERE r.item_id = i.id AND "
                       "r.status IN ('open','in_progress') AND r.start_error IS NOT NULL)", args).fetchone()["n"]
    return {"counts": counts,
            "attention": {"toReview": to_review, "revisionsReady": revisions,
                          "failedPosts": failed, "revisionsStuck": stuck}}


def niches() -> list[str]:
    rows = get_db().execute(
        "SELECT niche FROM cm_items WHERE niche != '' UNION SELECT default_niche FROM cm_accounts "
        "WHERE default_niche != ''").fetchall()
    return sorted({r[0] for r in rows}, key=str.lower)


def calendar(*, start: str, end: str, niche: str | None = None, content_type: str | None = None,
             platform: str | None = None, q: str | None = None) -> list[dict[str, Any]]:
    """Every placement with a date in [start, end): scheduled/queued ones by
    their schedule, published ones by when they went out."""
    where, args = _filters(niche=niche, content_type=content_type, platform=None, q=q)
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


# --- accounts ------------------------------------------------------------------

def account_dict(row: Any) -> dict[str, Any]:
    return {"id": row["id"], "platform": row["platform"], "platformLabel": platform_label(row["platform"]),
            "handle": row["handle"], "destinations": _loads(row["destinations_json"], []),
            "defaultNiche": row["default_niche"], "createdAt": row["created_at"]}


def list_accounts() -> list[dict[str, Any]]:
    rows = get_db().execute("SELECT * FROM cm_accounts ORDER BY platform, handle COLLATE NOCASE").fetchall()
    return [account_dict(r) for r in rows]


def get_account(account_id: str) -> dict[str, Any] | None:
    row = get_db().execute("SELECT * FROM cm_accounts WHERE id = ?", (account_id,)).fetchone()
    return account_dict(row) if row else None


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
        account = get_account(placement["accountId"]) if placement["accountId"] else None
        out.append({
            "placementId": placement["id"], "itemId": item["id"], "name": item["name"],
            "contentType": item["contentType"], "niche": item["niche"],
            "platform": placement["platform"], "account": account["handle"] if account else placement["accountLabel"],
            "destination": placement["destination"], "scheduledAt": placement["scheduledAt"],
            "timezone": placement["timezone"], "version": merged_version(item, placement),
            "media": item["media"],
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
