"""Jarvis's own Content Management tools, and Jarvis revising content itself.

The auto-revise test is the one that matters most: a change request sent to
Jarvis must start a REAL Background Job, whose REAL turn calls
`submit_content_revision` through the real permission gate and executor, and
the item must come back to Review with the new text — without the job parking
on a confirmation nobody is there to give.
"""

from __future__ import annotations

import time

import pytest
from starlette.testclient import TestClient

from jarvis import assembly, conversation
from jarvis.content_manager import lifecycle, store
from jarvis.db import get_db
from jarvis.db import reset_for_tests as reset_db
from jarvis.jobs import job_store, worker
from jarvis.capabilities import CapabilityRegistry
from jarvis.tools import load_tools

from scripted_model import ScriptedModel, install


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    conversation.reset_for_tests()
    assembly.reset_for_tests()
    yield
    worker.join_all(timeout=10)
    assembly.reset_for_tests()
    conversation.reset_for_tests()
    reset_db()


def _tool(name):
    return assembly.get_registry().get(name)


def _post():
    return lifecycle.submit(name="Why agents need review", content_type="text_post", niche="AI",
                            producer="Writer", fields={"body": "Agents are fast. Review keeps them honest.",
                                                       "hashtags": ["#AI"]})


def test_no_tool_can_approve_schedule_publish_or_delete_content():
    names = set(load_tools(CapabilityRegistry()))
    mine = {n for n in names if "content" in n and n not in (
        "share_content", "examine_content")}
    assert mine == {"submit_content_for_review", "submit_content_revision",
                    "list_content_change_requests", "content_status"}


def test_submit_tool_puts_content_in_review_and_never_further():
    result = _tool("submit_content_for_review").handler(
        name="Weekly AI digest", content_type="newsletter", niche="AI",
        fields={"subject": "This week in agents", "body": "Three things happened."})
    assert result["ok"] is True
    item = store.get_item(result["contentItemId"])
    assert item["stage"] == "review" and item["producer"] == "Jarvis"
    refused = _tool("submit_content_for_review").handler(name="x", content_type="video")
    assert refused["ok"] is False and "needs the actual file" in refused["error"]


def test_revision_tool_and_request_listing():
    item = _post()
    assert _tool("submit_content_revision").handler(content_item_id=item["id"], fields={"body": "x"})["ok"] is False
    lifecycle.request_changes(item["id"], what="Make it punchier", why="Too flat", assignee="jarvis")
    listed = _tool("list_content_change_requests").handler(assignee="jarvis")["requests"]
    assert listed[0]["what"] == "Make it punchier" and listed[0]["currentFields"]["body"].startswith("Agents")
    done = _tool("submit_content_revision").handler(content_item_id=item["id"],
                                                    fields={"body": "Agents move fast. Review makes them right."},
                                                    note="Punchier opener")
    assert done == {"ok": True, "revision": 2,
                    "note": "Revision 2 of “Why agents need review” is back in Review for them to check."}
    status = _tool("content_status").handler()
    assert status["counts"]["review"] == 1 and status["items"][0]["needs"] == ["Revision ready to review"]


def _wait(predicate, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def test_a_change_request_sent_to_jarvis_runs_a_real_job_that_revises_it():
    from jarvis.main import create_app

    item = _post()
    model = install(assembly, ScriptedModel())
    model.calls_tool("submit_content_revision", {
        "content_item_id": item["id"],
        "fields": {"body": "Agents move fast. Review makes them right."},
        "note": "Tightened the opening line"}, call_id="rev1")
    model.says("I've sent back a punchier version for review.")

    client = TestClient(create_app())
    response = client.post(f"/api/content-items/{item['id']}/request-changes",
                           json={"what": "Make the opening line punchier", "why": "It reads flat",
                                 "assignee": "jarvis"})
    assert response.status_code == 200
    # Read from history, not `openRequest`: the job can finish (and resolve the
    # request) before this response is even written — that is correct, and it
    # made this test flaky when it assumed the request would still be open.
    request = store.item_detail(item["id"])["requests"][0]
    assert request["jobId"], "a real background job was started"

    assert _wait(lambda: store.get_item(item["id"])["stage"] == "review"), \
        f"job ended as {job_store.get_job(request['jobId'])}"
    revised = store.item_detail(item["id"])
    assert revised["revision"] == 2
    assert revised["fields"]["body"] == "Agents move fast. Review makes them right."
    assert revised["fields"]["hashtags"] == ["#AI"], "fields the job did not touch are kept"
    assert revised["requests"][0]["status"] == "resolved"
    assert revised["revisions"][0]["by"] == "Jarvis"

    # What the model was actually asked, and what actually ran — not what it said.
    goal = model.requests[0]["messages"][-1]
    assert "Make the opening line punchier" in str(goal)
    worker.join_all(timeout=10)
    job = job_store.get_job(request["jobId"])
    assert job["status"] != "awaiting_decision", "the job must not park on the hand-back"
    rows = get_db().execute("SELECT kind FROM cm_events WHERE item_id = ? ORDER BY id", (item["id"],)).fetchall()
    assert [r["kind"] for r in rows][-2:] == ["revision_started", "revision_submitted"]


def test_a_job_that_cannot_start_is_shown_with_a_way_to_retry(monkeypatch):
    from jarvis.jobs import orchestrator
    from jarvis.jobs.orchestrator import AtCapacity
    from jarvis.main import create_app

    def full(**_):
        raise AtCapacity([])

    real_admit = orchestrator.admit
    monkeypatch.setattr("jarvis.jobs.orchestrator.admit", full)
    item = _post()
    client = TestClient(create_app())
    body = client.post(f"/api/content-items/{item['id']}/request-changes",
                       json={"what": "Shorter", "assignee": "jarvis"}).json()["item"]
    assert "as many background jobs" in body["openRequest"]["startError"]
    assert body["attention"] == ["The revision couldn't start"]
    assert store.summary()["attention"]["revisionsStuck"] == 1
    # Only this patch — `monkeypatch.undo()` would also undo the scratch fixture's
    # JARVIS_DATA_DIR, and the job started next would write into the real data/.
    monkeypatch.setattr("jarvis.jobs.orchestrator.admit", real_admit)
    install(assembly, ScriptedModel())
    retried = client.post(f"/api/content-change-requests/{body['openRequest']['id']}/start-jarvis").json()
    assert retried["ok"] is True and retried["item"]["openRequest"]["startError"] is None


def test_a_job_that_finishes_before_it_is_recorded_does_not_reopen_the_request(monkeypatch):
    """The race, forced: the job runs to the end INSIDE `admit()`, before the code
    that started it records the job's id. That order re-opened an answered request
    until the claim moved to before the job existed."""
    from jarvis.jobs import orchestrator
    from jarvis.main import create_app

    item = _post()
    model = install(assembly, ScriptedModel())
    model.calls_tool("submit_content_revision", {"content_item_id": item["id"],
                                                 "fields": {"body": "Short and sharp."}}, call_id="r")
    model.says("Done.")
    real_admit = orchestrator.admit

    def admit_and_finish(**kwargs):
        job = real_admit(**kwargs)
        worker.join_all(timeout=10)
        return job

    monkeypatch.setattr("jarvis.jobs.orchestrator.admit", admit_and_finish)
    body = TestClient(create_app()).post(f"/api/content-items/{item['id']}/request-changes",
                                         json={"what": "Shorter", "assignee": "jarvis"}).json()["item"]
    assert body["stage"] == "review" and body["openRequest"] is None
    request = store.item_detail(item["id"])["requests"][0]
    assert request["status"] == "resolved" and request["resolvedRevision"] == 2 and request["jobId"]
