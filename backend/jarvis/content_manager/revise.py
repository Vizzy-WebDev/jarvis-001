"""Jarvis doing a requested revision itself, as a Background Job.

Only when the person sent the change request to Jarvis. The job is an ordinary
one (visible on the Background Jobs screen); its goal carries the request and the
current text, and it hands the result back by calling `submit_content_revision`
— the same door an outside agent uses, so the item goes back to Review exactly
the same way whoever did the work.

Honest about its reach: Jarvis's models can rewrite text (captions, titles, a
post's body). They cannot re-cut a video, so the screen defaults media items to
the agent that made them.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..db import get_db
from . import lifecycle
from .kinds import TYPES, type_label
from .store import get_item

logger = logging.getLogger(__name__)


def _goal(item: dict[str, Any], request: Any) -> str:
    fields = TYPES[item["contentType"]]["fields"]
    current = {f: item["fields"].get(f) for f in fields if item["fields"].get(f) not in (None, "", [])}
    return (
        f"Revise a piece of content that the owner reviewed and asked to change.\n\n"
        f"Content item id: {item['id']}\n"
        f"What it is: a {type_label(item['contentType'])} called “{item['name']}”"
        f"{' in the ' + item['niche'] + ' niche' if item['niche'] else ''}.\n"
        f"What needs to change: {request['what']}\n"
        f"Why: {request['why'] or '(no reason given)'}\n\n"
        f"Its current supporting text (JSON):\n{json.dumps(current, ensure_ascii=False, indent=2)}\n\n"
        f"Fields this kind of content can have: {', '.join(fields)}.\n\n"
        "Rewrite only what the request asks for, keeping everything else as it is. Then call "
        "submit_content_revision exactly once, with content_item_id set to the id above, `fields` "
        "holding ONLY the fields you changed, and a one-sentence `note` saying what you changed. "
        "You cannot change video, images or audio. If the request needs that, do not call "
        "submit_content_revision; reply saying the media itself has to be changed by the agent "
        "that made it. Do not approve, schedule or publish anything."
    )


def start_jarvis_revision(request_id: str, *, event_bus: Any = None) -> dict[str, Any] | None:
    """Start the job for a change request assigned to Jarvis. Never raises: a job
    that cannot start is recorded on the request, where the screen shows it with
    a way to try again."""
    from ..jobs.orchestrator import AtCapacity, admit

    row = get_db().execute("SELECT * FROM cm_change_requests WHERE id = ?", (request_id,)).fetchone()
    if row is None or row["assignee"] != "jarvis" or row["status"] not in ("open", "in_progress"):
        return None
    item = get_item(row["item_id"])
    if item is None or item["deletedAt"] or item["stage"] != "changes_requested":
        return None
    lifecycle.record_job(request_id, starting=True, event_bus=event_bus)
    try:
        job = admit(title=f"Revise: {item['name']}"[:80], goal=_goal(item, row), kind="generic",
                    event_bus=event_bus)
    except AtCapacity:
        lifecycle.record_job(request_id, error="Jarvis is already running as many background jobs as it's "
                                               "allowed to. Try again once one finishes.", event_bus=event_bus)
        return None
    except Exception as err:  # noqa: BLE001 — shown to the person, never a crash
        logger.exception("could not start a revision job for %s", request_id)
        lifecycle.record_job(request_id, error=f"It couldn't start: {err}", event_bus=event_bus)
        return None
    lifecycle.record_job(request_id, job_id=job["id"], event_bus=event_bus)
    return job


def job_state(job_id: str | None) -> dict[str, Any] | None:
    """How Jarvis's revision job is doing, for the screen."""
    if not job_id:
        return None
    from ..jobs import job_store

    job = job_store.get_job(job_id)
    if job is None:
        return None
    return {"id": job["id"], "status": job.get("status"), "error": job.get("error"),
            "currentStep": job.get("currentStep"), "result": job.get("result")}
