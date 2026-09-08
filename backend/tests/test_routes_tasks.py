"""Scheduled tasks over HTTP.

The routes are a surface over `task_store`, which is tested on its own; what is
worth testing here is the edge behaviour a screen depends on — a real 404 rather
than a crash, the skill-name guard the store deliberately cannot make, and the
detail read that returns a task beside its own run history in one request.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from jarvis.scheduler import task_store


@pytest.fixture
def client(scratch):
    from jarvis.main import create_app

    return TestClient(create_app())


EVERY_MORNING = {"type": "daily", "time": "07:00"}
SAY_HELLO = {"type": "prompt", "prompt": "say good morning"}


def test_an_empty_install_answers_the_recorded_shape(client):
    assert client.get("/api/tasks").json() == {"tasks": [], "descriptions": {}}
    assert client.get("/api/task-runs").json() == {"runs": []}


def test_creating_reading_editing_and_deleting(client):
    created = client.post("/api/tasks", json={
        "title": "Morning hello", "recurrence": EVERY_MORNING, "action": SAY_HELLO}).json()
    assert created["ok"] is True
    task_id = created["task"]["id"]
    # The store computed the next occurrence; the route did not invent one.
    assert created["task"]["nextRunAt"]

    detail = client.get(f"/api/tasks/{task_id}").json()
    assert detail["task"]["title"] == "Morning hello"
    assert detail["runs"] == []
    assert detail["description"]

    edited = client.patch(f"/api/tasks/{task_id}", json={"title": "Morning greeting"}).json()
    assert edited["task"]["title"] == "Morning greeting"

    assert client.delete(f"/api/tasks/{task_id}").json() == {"ok": True}
    assert client.get("/api/tasks").json() == {"tasks": [], "descriptions": {}}


def test_turning_one_off_clears_its_next_run(client):
    """The screen shows "next run" from this field, so a paused task that still
    advertised a time would be actively misleading."""
    task_id = client.post("/api/tasks", json={
        "recurrence": EVERY_MORNING, "action": SAY_HELLO}).json()["task"]["id"]
    off = client.patch(f"/api/tasks/{task_id}", json={"enabled": False}).json()
    assert off["task"]["nextRunAt"] is None
    on = client.patch(f"/api/tasks/{task_id}", json={"enabled": True}).json()
    assert on["task"]["nextRunAt"]


def test_a_task_that_never_existed_is_a_clean_404(client):
    assert client.get("/api/tasks/nope").status_code == 404
    assert client.patch("/api/tasks/nope", json={"title": "x"}).status_code == 404
    assert client.post("/api/tasks/nope/run").status_code == 404
    # Deleting something already gone is what the caller wanted, not an error.
    assert client.delete("/api/tasks/nope").json() == {"ok": True}


def test_a_task_missing_its_recurrence_or_action_is_refused(client):
    assert client.post("/api/tasks", json={"action": SAY_HELLO}).status_code == 400
    assert client.post("/api/tasks", json={"recurrence": EVERY_MORNING}).status_code == 400


def test_a_skill_action_naming_something_that_does_not_exist_is_refused(client):
    """The store cannot make this check — it is a leaf and must never import the
    capability registry — so the route makes it, exactly as the original does."""
    refused = client.post("/api/tasks", json={
        "recurrence": EVERY_MORNING,
        "action": {"type": "skill", "skillName": "not_a_real_ability"}})
    assert refused.status_code == 400
    assert "not_a_real_ability" in refused.json()["error"]
    assert task_store.list_tasks() == []


def test_run_history_is_readable_whole_or_per_task(client):
    first = client.post("/api/tasks", json={
        "recurrence": EVERY_MORNING, "action": SAY_HELLO}).json()["task"]["id"]
    second = client.post("/api/tasks", json={
        "recurrence": EVERY_MORNING, "action": SAY_HELLO}).json()["task"]["id"]
    task_store.record_run({"taskId": first, "ok": True, "summary": "done"})
    task_store.record_run({"taskId": second, "ok": False, "error": "no model"})

    assert len(client.get("/api/task-runs").json()["runs"]) == 2
    mine = client.get("/api/task-runs", params={"taskId": first}).json()["runs"]
    assert [r["taskId"] for r in mine] == [first]
    assert client.get(f"/api/tasks/{first}").json()["runs"][0]["summary"] == "done"


def test_each_task_carries_the_plain_english_sentence_for_its_schedule(client):
    """The row shows a title AND when it runs. The sentence comes from the same
    `describe()` the spoken read-back uses, never re-derived in the browser."""
    task_id = client.post("/api/tasks", json={
        "title": "Morning hello", "recurrence": {"type": "weekdays", "time": "07:00"},
        "action": SAY_HELLO}).json()["task"]["id"]

    listed = client.get("/api/tasks").json()
    assert "weekday" in listed["descriptions"][task_id].lower()
    # The task itself is untouched: the sentence is a sibling, which is what
    # keeps the recorded shape byte-identical.
    assert "description" not in listed["tasks"][0]

    assert listed["descriptions"][task_id] == client.get(f"/api/tasks/{task_id}").json()["description"]
