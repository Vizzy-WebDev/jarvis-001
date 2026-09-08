"""Background jobs over HTTP.

The orchestrator and the worker are tested on their own; these drive the routes
with the worker stubbed out, because what is being checked is that the routes ask
the orchestrator rather than deciding anything themselves — capacity, the parked
start for desktop work, and the trace-backed refusal to restart.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from jarvis.jobs import job_store, orchestrator


@pytest.fixture
def client(scratch, monkeypatch):
    from jarvis.main import create_app

    # Nothing here is testing whether work gets done — only what the routes ask
    # for. A real worker would make real model calls on a background thread.
    started: list[str] = []
    monkeypatch.setattr(orchestrator.worker, "run_in_background",
                        lambda job_id, event_bus=None: started.append(job_id))
    test_client = TestClient(create_app())
    test_client.started = started  # type: ignore[attr-defined]
    return test_client


def test_an_empty_install_answers_the_recorded_shape(client):
    assert client.get("/api/jobs").json() == {"jobs": []}


def test_an_unknown_job_is_a_404_with_the_recorded_message(client):
    answer = client.get("/api/jobs/zzz-does-not-exist-zzz")
    assert answer.status_code == 404
    assert answer.json() == {"ok": False, "error": "Unknown job."}


def test_creating_one_starts_it(client):
    created = client.post("/api/jobs", json={"title": "Read the archive",
                                             "goal": "find every mention"}).json()
    assert created["ok"] is True
    assert created["job"]["status"] == "queued"
    assert client.started == [created["job"]["id"]]


def test_a_job_with_no_goal_is_refused_before_anything_is_spent(client):
    assert client.post("/api/jobs", json={"title": "x"}).status_code == 400


def test_desktop_work_is_created_parked_and_only_resume_starts_it(client):
    """Autonomously operating the real machine is exactly the outward-facing,
    hard-to-undo work that has to come back to the owner first. There is no
    separate confirm mechanism for it — the ordinary "keep going" is the only
    thing that ever starts it."""
    created = client.post("/api/jobs", json={"title": "Tidy the desktop",
                                             "goal": "close everything",
                                             "kind": "computer"}).json()
    assert created["job"]["status"] == "awaiting_decision"
    assert created["job"]["startedAt"] is None
    assert client.started == []

    # It raised an outbox row rather than starting: somebody has to answer.
    detail = client.get(f"/api/jobs/{created['job']['id']}").json()
    assert detail["outbox"] and detail["outbox"][0]["tier"] == 1

    resumed = client.post(f"/api/jobs/{created['job']['id']}/resume").json()
    assert resumed["job"]["status"] == "queued"
    assert client.started == [created["job"]["id"]]


def test_guidance_given_on_resume_is_recorded_in_the_trace(client):
    created = client.post("/api/jobs", json={"goal": "keep looking"}).json()["job"]
    client.post(f"/api/jobs/{created['id']}/resume", json={"guidance": "try the archive"})

    trace = client.get(f"/api/jobs/{created['id']}").json()["trace"]
    assert any(row["detail"] == "try the archive" for row in trace)


def test_no_room_is_a_409_naming_what_is_in_the_way(client):
    """Not an error: everything is working, there is simply no room. The screen
    can say which ones are holding it up."""
    from jarvis.prefs import set_prefs

    set_prefs({"maxBackgroundJobs": 1})
    first = client.post("/api/jobs", json={"title": "First", "goal": "one"}).json()

    answer = client.post("/api/jobs", json={"title": "Second", "goal": "two"})
    assert answer.status_code == 409
    assert [j["id"] for j in answer.json()["active"]] == [first["job"]["id"]]


def test_the_detail_reads_the_job_beside_what_it_actually_did(client):
    created = client.post("/api/jobs", json={"goal": "look something up"}).json()["job"]
    job_store.append_trace(created["id"], phase="intent", effect="read",
                           kind="tool", summary="reading a page")

    detail = client.get(f"/api/jobs/{created['id']}").json()
    assert detail["job"]["id"] == created["id"]
    assert [row["summary"] for row in detail["trace"]] == ["reading a page"]


def test_restarting_refuses_on_a_trace_that_reached_the_outside_world(client):
    """Repeating something that already left the machine is not something a
    retry can take back, so the record decides and the route reports it."""
    created = client.post("/api/jobs", json={"goal": "send the email"}).json()["job"]
    job_store.update_job(created["id"], {"recovery": "unrecoverable"})

    refused = client.post(f"/api/jobs/{created['id']}/restart").json()
    assert refused["ok"] is False and refused["reason"] == "unrecoverable"

    forced = client.post(f"/api/jobs/{created['id']}/restart", json={"force": True}).json()
    assert forced["ok"] is True and forced["job"]["status"] == "queued"


def test_restarting_clears_what_the_last_attempt_left_behind(client):
    created = client.post("/api/jobs", json={"goal": "try again"}).json()["job"]
    job_store.update_job(created["id"], {"result": "half an answer", "error": "gave up",
                                         "progress": 40})

    restarted = client.post(f"/api/jobs/{created['id']}/restart").json()["job"]
    assert restarted["result"] is None and restarted["error"] is None
    assert restarted["progress"] == 0


def test_discarding_stops_it_whatever_state_it_was_in(client):
    created = client.post("/api/jobs", json={"goal": "never mind"}).json()["job"]
    discarded = client.post(f"/api/jobs/{created['id']}/discard").json()
    assert discarded["job"]["status"] == "cancelled"
    assert discarded["job"]["finishedAt"]


def test_filtering_by_several_statuses_at_once(client):
    running = client.post("/api/jobs", json={"goal": "a"}).json()["job"]
    done = client.post("/api/jobs", json={"goal": "b"}).json()["job"]
    job_store.update_job(done["id"], {"status": "succeeded"})

    both = client.get("/api/jobs?status=queued,succeeded").json()["jobs"]
    assert {j["id"] for j in both} == {running["id"], done["id"]}
    assert [j["id"] for j in client.get("/api/jobs?status=succeeded").json()["jobs"]] \
        == [done["id"]]


def test_every_action_on_an_unknown_job_is_a_404(client):
    for path in ("resume", "restart", "discard"):
        assert client.post(f"/api/jobs/nope/{path}").status_code == 404
