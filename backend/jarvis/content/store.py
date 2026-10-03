"""One record per thing shared: what it is, what has been read of it, what has
been asked about it.

Three fields carry the whole design:

* `identity` — what it IS, from a free glance. No model call ever wrote this.
* `material` — a cache, filled only once something has actually been examined.
  Text sources keep their full text (cheap and always safe to re-read). Video,
  audio and images additionally keep `observations`: a neutral note of what a
  model really perceived, so a second question does not re-watch the thing.
* `findings` — one entry per question asked and its answer. Nothing here is
  "the" verdict; there can be many, and they are dated.

A leaf: the JSON store and the clock. Tools import this directly, and the tool
loader imports every tool, so nothing reachable from here may lead back to it.
"""

from __future__ import annotations

import uuid
from typing import Any

from ..jscompat import now_iso
from ..store import read_json, write_json

FILE = "content"


def _load() -> dict[str, Any]:
    data = read_json(FILE, {"content": []})
    if not isinstance(data, dict) or not isinstance(data.get("content"), list):
        return {"content": []}
    return data


def _save(data: dict[str, Any]) -> None:
    write_json(FILE, data)


def _new_id() -> str:
    return f"ct{uuid.uuid4().hex[:12]}"


def list_content() -> list[dict[str, Any]]:
    return sorted(_load()["content"], key=lambda c: str(c.get("createdAt")), reverse=True)


def get_content(content_id: str) -> dict[str, Any] | None:
    return next((c for c in _load()["content"] if c.get("id") == content_id), None)


def create_content(*, source: dict[str, Any], identity: dict[str, Any] | None,
                   session_id: str) -> dict[str, Any]:
    stamp = now_iso()
    record = {
        "id": _new_id(),
        # Which conversation this belongs to. Findings are looked up by session
        # first, so two things shared in two conversations never get crossed.
        "sessionId": session_id,
        "source": source or {"kind": "text"},
        "identity": identity,
        "material": None,
        "findings": [],
        "createdAt": stamp,
        "updatedAt": stamp,
    }
    data = _load()
    data["content"].append(record)
    _save(data)
    return record


def update_content(content_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    """None if it was deleted mid-job — a normal thing for the user to do, not
    an error to raise about."""
    data = _load()
    for index, record in enumerate(data["content"]):
        if record.get("id") != content_id:
            continue
        merged = {**record, **patch, "id": content_id, "updatedAt": now_iso()}
        data["content"][index] = merged
        _save(data)
        return merged
    return None


def add_finding(content_id: str, *, request: str, answer: str, intake: str | None,
                sources: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """Appends one question and its answer. Separate from `update_content` so two
    findings recorded close together cannot overwrite each other's history."""
    record = get_content(content_id)
    if record is None:
        return None
    finding = {
        "id": f"fd{uuid.uuid4().hex[:10]}",
        "request": (request or "").strip(),
        "answer": answer or "",
        "intake": intake or ((record.get("material") or {}).get("intake")),
        "sources": sources or [],
        "at": now_iso(),
    }
    updated = update_content(content_id, {"findings": [*record.get("findings", []), finding]})
    return {"record": updated, "finding": finding} if updated else None


def cache_material(content_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    record = get_content(content_id)
    if record is None:
        return None
    return update_content(content_id, {"material": {**(record.get("material") or {}), **patch}})


def delete_content(content_id: str) -> None:
    data = _load()
    data["content"] = [c for c in data["content"] if c.get("id") != content_id]
    _save(data)


def latest_in_session(session_id: str) -> dict[str, Any] | None:
    """What a follow-up most likely refers to, WITHIN one conversation.

    Session-scoped deliberately: a global "most recent" resolves "what did that
    say?" to something shared in a completely different conversation.
    """
    mine = [c for c in _load()["content"] if c.get("sessionId") == session_id]
    return max(mine, key=lambda c: str(c.get("updatedAt")), default=None)
