"""One record per idea being talked through.

`decisions` is the important field. It is also the deliberate mitigation for
conversation history being finite: the chat scrolls away, but what was actually
settled does not, and both the plan and the handoff prompts are written from it.

A leaf: the JSON store and the clock.
"""

from __future__ import annotations

import uuid
from typing import Any

from ..jscompat import now_iso
from ..store import read_json, write_json

FILE = "projects"


def _load() -> dict[str, Any]:
    data = read_json(FILE, {"projects": []})
    if not isinstance(data, dict) or not isinstance(data.get("projects"), list):
        return {"projects": []}
    return data


def _save(data: dict[str, Any]) -> None:
    write_json(FILE, data)


def list_projects() -> list[dict[str, Any]]:
    return sorted(_load()["projects"], key=lambda p: str(p.get("updatedAt")), reverse=True)


def get_project(project_id: str) -> dict[str, Any] | None:
    return next((p for p in _load()["projects"] if p.get("id") == project_id), None)


def create_project(*, idea: str, session_id: str, title: str | None = None) -> dict[str, Any]:
    text = (idea or "").strip()
    if not text:
        raise ValueError("A project needs an idea to start from.")
    stamp = now_iso()
    project = {
        "id": f"pj{uuid.uuid4().hex[:12]}",
        "sessionId": session_id,
        "title": (title or "").strip() or " ".join(text.split()[:8]),
        "idea": text,
        "decisions": [],
        "research": None,
        "plan": None,
        "prompts": None,
        "promptOrder": None,
        "target": None,
        # Which model did which step, so a plan written by a weak fallback is
        # not silently indistinguishable from one written by the good model.
        "history": [],
        "error": None,
        "createdAt": stamp,
        "updatedAt": stamp,
    }
    data = _load()
    data["projects"].append(project)
    _save(data)
    return project


def update_project(project_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    """None when it has been deleted mid-job — the user is allowed to bin a
    project while something is still working on it."""
    data = _load()
    for index, project in enumerate(data["projects"]):
        if project.get("id") != project_id:
            continue
        merged = {**project, **patch, "id": project_id, "updatedAt": now_iso()}
        data["projects"][index] = merged
        _save(data)
        return merged
    return None


def add_decision(project_id: str, text: str) -> dict[str, Any] | None:
    """Appends one settled decision. Separate from `update_project` so two noted
    close together cannot overwrite each other."""
    project = get_project(project_id)
    if project is None:
        return None
    clean = (text or "").strip()
    if not clean:
        return project
    return update_project(project_id, {
        "decisions": [*project.get("decisions", []), {"text": clean, "at": now_iso()}]})


def record_step(project_id: str, step: str, model_id: str | None) -> dict[str, Any] | None:
    project = get_project(project_id)
    if project is None:
        return None
    return update_project(project_id, {
        "history": [*project.get("history", []),
                    {"step": step, "modelId": model_id, "at": now_iso()}]})


def delete_project(project_id: str) -> None:
    data = _load()
    data["projects"] = [p for p in data["projects"] if p.get("id") != project_id]
    _save(data)


def latest_in_session(session_id: str) -> dict[str, Any] | None:
    """The project a follow-up most likely belongs to, within ONE conversation."""
    mine = [p for p in _load()["projects"] if p.get("sessionId") == session_id]
    return max(mine, key=lambda p: str(p.get("updatedAt")), default=None)
