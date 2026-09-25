"""Self-improvement over HTTP.

The pipeline is tested on its own. What is worth testing here is that the routes
did not become a second place the rules are decided: a proposal that may not be
applied is refused by `apply`, not by a check in the route; an undo that would
overwrite a later decision refuses and says so; and the change log stays honest
about what a rule was before.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from jarvis.improvement import store
from jarvis.prefs import get_prefs


@pytest.fixture
def client(scratch):
    from jarvis.main import create_app

    return TestClient(create_app())


def rule_proposal(**over):
    payload = {"kind": "rule", "title": "Say less on the first pass",
               "payload": {"text": "Say less on the first pass", "scope": "general"},
               "source_tier": 1}
    payload.update(over)
    return store.create_proposal(**payload)


def test_an_empty_install_answers_the_recorded_shapes(client):
    assert client.get("/api/improvement/proposals").json() == {"proposals": []}
    assert client.get("/api/improvement/rules").json() == {"rules": []}
    assert client.get("/api/improvement/lessons").json() == {"lessons": []}
    assert client.get("/api/improvement/changes").json() == {"changes": []}
    assert client.get("/api/improvement/outcomes").json() == {"outcomes": []}


def test_status_reads_the_live_prefs_and_the_real_budgets(client):
    status = client.get("/api/improvement/status").json()
    assert status["enabled"] is get_prefs()["improvementEnabled"]
    assert status["trust"] == get_prefs()["improvementTrust"]
    assert status["dailyBudgetRemaining"] == store.DAILY_BUDGET
    assert status["weeklyBudgetRemaining"] == store.WEEKLY_BUDGET
    assert status["unreviewedOutcomes"] == 0 and status["pendingProposals"] == 0


def test_the_rows_come_back_in_the_spelling_the_rest_of_the_api_uses(client):
    """Database columns are snake_case and everything else in this API is not.
    Translating at the edge keeps the store's shapes as its own tests read them."""
    store.create_lesson(text="Checks the trace before claiming", source_tier=1)
    lesson = client.get("/api/improvement/lessons").json()["lessons"][0]
    assert "sourceTier" in lesson and "source_tier" not in lesson
    assert "createdAt" in lesson


def test_approving_a_rule_creates_it_live_and_logs_the_change(client):
    proposal = rule_proposal()
    answer = client.post(f"/api/improvement/proposals/{proposal['id']}/approve").json()
    assert answer["ok"] is True

    rules = client.get("/api/improvement/rules").json()["rules"]
    assert [r["text"] for r in rules] == ["Say less on the first pass"]
    assert len(client.get("/api/improvement/changes").json()["changes"]) == 1


def test_a_code_proposal_is_refused_by_the_policy_not_by_the_route(client):
    """Jarvis never edits its own code. The refusal has to come from the one
    place that decides it, or there are two answers to the same question and
    eventually they disagree."""
    proposal = store.create_proposal(kind="code", title="Rewrite the router",
                                     payload={"text": "..."}, source_tier=1)
    answer = client.post(f"/api/improvement/proposals/{proposal['id']}/approve")
    assert answer.status_code == 400
    assert "person" in answer.json()["error"]
    assert client.get("/api/improvement/rules").json()["rules"] == []


def test_a_code_proposal_gets_a_brief_instead_and_that_is_the_approval(client):
    proposal = store.create_proposal(kind="code", title="Rewrite the router",
                                     rationale="It is doing two jobs",
                                     payload={"text": "..."}, source_tier=1)
    answer = client.post(f"/api/improvement/proposals/{proposal['id']}/implementation-prompt",
                         json={"target": "some coding assistant"}).json()
    assert answer["ok"] is True
    assert "Rewrite the router" in answer["prompt"]
    # Recorded on the proposal, so the screen can show it again without
    # regenerating it.
    assert store.get_proposal(proposal["id"])["implementation_prompt"]


def test_an_unknown_proposal_is_a_404_on_every_path(client):
    for path in ("approve", "reject", "restore", "implementation-prompt"):
        assert client.post(f"/api/improvement/proposals/nope/{path}").status_code == 404


def test_rejecting_then_restoring_puts_it_back_as_pending_not_applied(client):
    proposal = rule_proposal()
    client.post(f"/api/improvement/proposals/{proposal['id']}/reject")
    assert client.get("/api/improvement/proposals").json()["proposals"] == []

    client.post(f"/api/improvement/proposals/{proposal['id']}/restore")
    assert len(client.get("/api/improvement/proposals").json()["proposals"]) == 1
    assert client.get("/api/improvement/rules").json()["rules"] == []


def test_muting_a_rule_records_what_it_was_so_it_can_be_undone(client):
    proposal = rule_proposal()
    client.post(f"/api/improvement/proposals/{proposal['id']}/approve")
    rule = client.get("/api/improvement/rules").json()["rules"][0]

    muted = client.post(f"/api/improvement/rules/{rule['id']}/toggle").json()
    assert muted["rule"]["active"] == 0
    change = client.get("/api/improvement/changes").json()["changes"][0]
    assert change["before"]["active"] is True and change["after"]["active"] is False


def test_editing_a_rules_text_rides_the_same_undo_machinery(client):
    proposal = rule_proposal()
    client.post(f"/api/improvement/proposals/{proposal['id']}/approve")
    rule = client.get("/api/improvement/rules").json()["rules"][0]

    client.patch(f"/api/improvement/rules/{rule['id']}", json={"text": "Say much less"})
    assert client.get("/api/improvement/rules").json()["rules"][0]["text"] == "Say much less"
    change = client.get("/api/improvement/changes").json()["changes"][0]
    assert change["before"]["text"] == "Say less on the first pass"


def test_a_rule_with_no_text_is_refused(client):
    proposal = rule_proposal()
    client.post(f"/api/improvement/proposals/{proposal['id']}/approve")
    rule = client.get("/api/improvement/rules").json()["rules"][0]
    assert client.patch(f"/api/improvement/rules/{rule['id']}", json={"text": " "}).status_code == 400


def test_a_rule_is_archived_before_it_can_be_deleted(client):
    """A change row's undo needs the rule it points at to still exist, which is
    why nothing offers a hard delete from the live list."""
    proposal = rule_proposal()
    client.post(f"/api/improvement/proposals/{proposal['id']}/approve")
    rule = client.get("/api/improvement/rules").json()["rules"][0]

    client.post(f"/api/improvement/rules/{rule['id']}/archive")
    assert client.get("/api/improvement/rules").json()["rules"] == []
    assert len(client.get("/api/improvement/rules?includeArchived=true").json()["rules"]) == 1

    client.post(f"/api/improvement/rules/{rule['id']}/restore")
    assert len(client.get("/api/improvement/rules").json()["rules"]) == 1

    client.post(f"/api/improvement/rules/{rule['id']}/archive")
    client.delete(f"/api/improvement/rules/{rule['id']}")
    assert client.get("/api/improvement/rules?includeArchived=true").json()["rules"] == []


def test_undoing_removes_the_rule_it_created(client):
    proposal = rule_proposal()
    client.post(f"/api/improvement/proposals/{proposal['id']}/approve")
    change = client.get("/api/improvement/changes").json()["changes"][0]

    assert client.post(f"/api/improvement/changes/{change['id']}/undo").json()["ok"] is True
    assert client.get("/api/improvement/rules").json()["rules"] == []


def test_an_undo_that_would_overwrite_a_later_decision_refuses_and_says_so(client):
    """A 200, not an error: a real question got a real answer. Returning 4xx
    would make a screen treat a normal branch as a failure."""
    proposal = rule_proposal()
    client.post(f"/api/improvement/proposals/{proposal['id']}/approve")
    rule = client.get("/api/improvement/rules").json()["rules"][0]
    change = client.get("/api/improvement/changes").json()["changes"][0]

    # The person muted it themselves since. Undoing blind would silently
    # overwrite that.
    client.post(f"/api/improvement/rules/{rule['id']}/toggle")

    refused = client.post(f"/api/improvement/changes/{change['id']}/undo")
    assert refused.status_code == 200
    assert refused.json()["ok"] is False and refused.json()["reason"] == "changed_since"
    assert client.get("/api/improvement/rules?includeArchived=true").json()["rules"]

    forced = client.post(f"/api/improvement/changes/{change['id']}/undo", json={"force": True})
    assert forced.json()["ok"] is True


def test_lessons_archive_restore_and_delete(client):
    lesson = store.create_lesson(text="Reads the trace first", source_tier=1)
    client.post(f"/api/improvement/lessons/{lesson['id']}/archive")
    assert client.get("/api/improvement/lessons").json()["lessons"] == []
    assert len(client.get("/api/improvement/lessons?status=archived").json()["lessons"]) == 1

    client.post(f"/api/improvement/lessons/{lesson['id']}/restore")
    assert len(client.get("/api/improvement/lessons").json()["lessons"]) == 1

    client.delete(f"/api/improvement/lessons/{lesson['id']}")
    assert client.get("/api/improvement/lessons?status=").json()["lessons"] == []


def test_the_evidence_behind_a_lesson_can_be_read_back_by_id(client):
    """What separates "it decided this" from "it decided this, and here is why"."""
    outcome = store.record_outcome(source="job", title="Looked up the weather",
                                   status="succeeded")
    found = client.post("/api/improvement/outcomes/lookup",
                        json={"ids": [outcome["id"], "not-a-real-id"]}).json()
    assert [o["title"] for o in found["outcomes"]] == ["Looked up the weather"]
