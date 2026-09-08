"""Self-Improvement (§—): capture, reflect, synthesize, apply, undo.

The policy is a truth table, since it is pure and its floors are the thing most
worth never letting drift. The chain is driven through the real `ask()` path
against a stub model, because the claims that matter are about what CANNOT
happen: a single event becoming a rule, an outside-sourced lesson riding in on
tier-1 evidence, an undo quietly overwriting what the user did themselves.
"""

from __future__ import annotations

import json

import pytest

from jarvis.db import reset_for_tests as reset_db
from jarvis.gateway import availability, connections, registry as model_registry
from jarvis.improvement import apply, capture, store
from jarvis.improvement.domains import is_excluded
from jarvis.improvement.policy import (
    AUTO_APPLY, MIN_EVIDENCE_BY_TRUST, REQUIRE_APPROVAL, decide,
)
from jarvis.improvement.reflect import reflect
from jarvis.improvement.synthesize import synthesize
from jarvis.prefs import set_prefs

from stub_openai_server import StubModelServer


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    availability.reset_for_tests()
    yield
    availability.reset_for_tests()
    reset_db()


@pytest.fixture
def stub():
    server = StubModelServer()
    server.base_url = server.start()
    conn = connections.add_connection(adapter="openai-compatible", base_url=server.base_url,
                                      label="stub", provider="custom", kind="local",
                                      key_required=False)
    model_registry.add_model(connection_id=conn["id"], model="stub-model")
    yield server
    server.stop()


# --- the policy, as a truth table --------------------------------------------

@pytest.mark.parametrize("trust", list(MIN_EVIDENCE_BY_TRUST))
@pytest.mark.parametrize("kind", ["skill", "code", "idea", None])
def test_anything_needing_real_work_always_asks(trust, kind):
    """Jarvis does not edit itself, at any setting."""
    assert decide({"kind": kind, "sourceTier": 1, "evidence": [1, 2, 3]}, trust) \
        == REQUIRE_APPROVAL


@pytest.mark.parametrize("trust", list(MIN_EVIDENCE_BY_TRUST))
@pytest.mark.parametrize("tier", [2, 3, 4, None, "two"])
def test_anything_read_from_outside_always_asks(trust, tier):
    """Only what Jarvis observed itself may apply itself, however solid the rest
    looks. An unreadable tier is treated as outside, not as tier one."""
    assert decide({"kind": "rule", "sourceTier": tier, "evidence": [1, 2, 3]}, trust) \
        == REQUIRE_APPROVAL


def test_a_tier_that_survived_json_as_a_string_is_still_tier_one():
    """Deliberate rather than accidental: proposals round-trip through JSON, and
    "1" is the number one however it was stored."""
    assert decide({"kind": "rule", "sourceTier": "1", "evidence": [1, 2]}, "balanced") \
        == AUTO_APPLY


@pytest.mark.parametrize("trust", list(MIN_EVIDENCE_BY_TRUST))
def test_a_conflict_always_asks(trust):
    assert decide({"kind": "rule", "sourceTier": 1, "evidence": [1, 2],
                   "conflictWith": "rule_1"}, trust) == REQUIRE_APPROVAL


def test_evidence_is_the_damping_floor_beneath_trust():
    """One failure can never mint a permanent rule."""
    single = {"kind": "rule", "sourceTier": 1, "evidence": ["one"]}
    assert decide(single, "balanced") == REQUIRE_APPROVAL
    assert decide(single, "auto") == AUTO_APPLY
    assert decide({**single, "evidence": ["one", "two"]}, "balanced") == AUTO_APPLY
    assert decide(single, "ask") == REQUIRE_APPROVAL


def test_ask_is_infinity_so_nothing_applies_itself():
    assert MIN_EVIDENCE_BY_TRUST["ask"] == float("inf")
    assert decide({"kind": "rule", "sourceTier": 1, "evidence": list(range(50))}, "ask") \
        == REQUIRE_APPROVAL


# --- excluded domains --------------------------------------------------------

def test_the_excluded_filter_is_broad_but_not_absurd():
    assert is_excluded("my wife and I argued about money")
    assert is_excluded("I've been feeling low lately")
    assert not is_excluded("I have a good relationship with this codebase")
    assert not is_excluded("the deployment failed again")


# --- capture -----------------------------------------------------------------

def test_a_terminal_job_records_once_however_often_it_is_reported():
    """Both the supervisor and the worker can report the same terminal status,
    and that must not count as two pieces of evidence."""
    job = {"id": "job_1", "title": "Find flights", "status": "stalled", "goal": "g",
           "kind": "research", "retries": 1, "error": "went nowhere"}
    assert capture.record_job_outcome(job) is not None
    assert capture.record_job_outcome(job) is None
    assert store.count_unreviewed_outcomes() == 1


def test_a_job_still_running_is_not_an_outcome():
    assert capture.record_job_outcome({"id": "j", "status": "running", "title": "t"}) is None


def test_each_correction_is_its_own_event():
    """Unlike a terminal status, a correction is never a repeat."""
    for _ in range(3):
        capture.note_correction("no, that's not what I asked for")
    assert store.count_unreviewed_outcomes() == 3


def test_ordinary_conversation_is_not_a_correction():
    assert capture.note_correction("what's the weather like") is None
    assert capture.note_correction("that's great, thanks") is None


# --- reflect -----------------------------------------------------------------

def seed_outcomes(count: int) -> None:
    for i in range(count):
        store.record_outcome(source="job", source_ref=f"j{i}", title=f"Job {i}",
                             status="failed", error="timed out")


def test_reflection_waits_until_there_is_enough_to_reflect_on(stub):
    seed_outcomes(2)
    assert reflect()["ran"] is False


def test_reflection_files_lessons_that_cite_real_outcomes(stub):
    seed_outcomes(6)
    outcome_ids = [o["id"] for o in store.list_unreviewed_outcomes()]
    stub.says(json.dumps({"lessons": [
        {"text": "run_code times out without an explicit limit", "scope": "tool:run_code",
         "evidence": outcome_ids[:2], "confidence": 0.7},
        {"text": "a made-up lesson", "evidence": ["out_never_existed"]},
    ]}))

    result = reflect()
    assert result["ran"] is True
    # The second is dropped: a lesson citing an outcome that never happened is
    # worse than no lesson.
    assert [l["text"] for l in result["lessons"]] == \
        ["run_code times out without an explicit limit"]
    assert store.count_unreviewed_outcomes() == 0


def test_material_is_kept_when_no_model_can_reflect_on_it():
    """The original discarded outcomes whenever its one call failed — on a
    roster that is routinely all rate-limited, that is silent data loss."""
    seed_outcomes(6)
    result = reflect()
    assert result["ran"] is False and result["reason"] == "no model available"
    assert store.count_unreviewed_outcomes() == 6


def test_the_daily_budget_stops_a_busy_day_from_reflecting_every_tick(stub):
    seed_outcomes(6)
    stub.says(json.dumps({"lessons": []}))
    assert reflect()["ran"] is True
    seed_outcomes(6)
    # Cadence and budget both apply; either one alone would be enough here.
    assert reflect()["ran"] is False


# --- synthesize --------------------------------------------------------------

def two_lessons_from(outcome_ids: list[str]) -> None:
    store.create_lesson(text="run_code needs a timeout", scope="tool:run_code",
                        evidence=outcome_ids[:1])
    store.create_lesson(text="long jobs stall without a limit", scope="tool:run_code",
                        evidence=outcome_ids[1:2] or outcome_ids[:1])


def test_two_lessons_from_the_same_event_do_not_make_a_pattern(stub):
    """Two lessons drawn from one event are one event described twice."""
    outcome = store.record_outcome(source="job", source_ref="j1", title="Job", status="failed")
    two_lessons_from([outcome["id"]])
    lessons = store.list_lessons()
    stub.says(json.dumps({"proposals": [
        {"title": "Always set a timeout", "kind": "rule",
         "lessons": [l["id"] for l in lessons]}]}))

    result = synthesize(force=True)
    assert result["proposals"] == []
    assert "same event" in result["rejected"][0]


def test_a_real_pattern_becomes_a_proposal(stub):
    ids = [store.record_outcome(source="job", source_ref=f"j{i}", title="Job",
                                status="failed")["id"] for i in range(2)]
    two_lessons_from(ids)
    lessons = store.list_lessons()
    stub.says(json.dumps({"proposals": [
        {"title": "Always set a timeout", "kind": "rule", "text": "Set a timeout.",
         "lessons": [l["id"] for l in lessons]}]}))

    result = synthesize(force=True)
    assert [p["title"] for p in result["proposals"]] == ["Always set a timeout"]
    assert result["proposals"][0]["evidence"] == sorted(ids)


def test_an_outside_sourced_lesson_cannot_ride_in_on_tier_one_evidence(stub):
    """The proposal's tier is the WORST of its lessons, so the tier floor still
    applies to the pattern as a whole."""
    ids = [store.record_outcome(source="job", source_ref=f"j{i}", title="Job",
                                status="failed")["id"] for i in range(2)]
    store.create_lesson(text="observed here", evidence=[ids[0]], source_tier=1)
    store.create_lesson(text="read on a forum", evidence=[ids[1]], source_tier=3)
    lessons = store.list_lessons()
    stub.says(json.dumps({"proposals": [
        {"title": "Do the thing", "kind": "rule", "lessons": [l["id"] for l in lessons]}]}))

    proposal = synthesize(force=True)["proposals"][0]
    assert proposal["sourceTier"] == 3
    assert decide(proposal, "auto") == REQUIRE_APPROVAL


# --- apply and undo ----------------------------------------------------------

def test_applying_a_rule_records_what_it_was_and_what_it_became():
    proposal = store.create_proposal(kind="rule", title="Always set a timeout",
                                     payload={"text": "Set a timeout."}, evidence=["a", "b"])
    change = apply.apply_proposal(proposal["id"])
    assert change["before"] is None
    assert change["after"]["text"] == "Set a timeout."
    assert store.active_rules_text() == "- Set a timeout."


def test_a_setting_change_can_be_undone_to_exactly_what_it_was():
    set_prefs({"balance": "balanced"})
    proposal = store.create_proposal(kind="setting", title="Prefer faster models",
                                     payload={"key": "balance", "value": "fast"},
                                     evidence=["a", "b"])
    change = apply.apply_proposal(proposal["id"])
    from jarvis.prefs import get_prefs

    assert get_prefs()["balance"] == "fast"
    apply.undo_change(change["id"])
    assert get_prefs()["balance"] == "balanced"


def test_undo_refuses_rather_than_overwriting_what_the_user_did_since():
    proposal = store.create_proposal(kind="rule", title="A rule",
                                     payload={"text": "Do the thing."}, evidence=["a", "b"])
    change = apply.apply_proposal(proposal["id"])
    store.update_rule_text(change["target"], "the user edited this themselves")

    with pytest.raises(apply.UndoRefused):
        apply.undo_change(change["id"])
    # And proceeds when the user has been shown that and said so anyway.
    apply.undo_change(change["id"], force=True)


def test_an_undo_is_not_itself_undoable():
    """Bringing something back means approving it again, which is what keeps
    every before/after pair in the log honest."""
    proposal = store.create_proposal(kind="rule", title="A rule",
                                     payload={"text": "x"}, evidence=["a", "b"])
    change = apply.apply_proposal(proposal["id"])
    undo = apply.undo_change(change["id"])
    with pytest.raises(ValueError, match="can't itself be undone"):
        apply.undo_change(undo["id"])


def test_a_proposal_needing_real_code_is_never_applied():
    proposal = store.create_proposal(kind="code", title="Rewrite the scheduler",
                                     evidence=["a", "b"])
    with pytest.raises(ValueError, match="needs real work by a person"):
        apply.apply_proposal(proposal["id"])


def test_a_code_proposal_produces_a_brief_instead():
    from jarvis.improvement.implementation_prompt import build

    proposal = store.create_proposal(kind="code", title="Add a retry to the exporter",
                                     rationale="It fails on slow networks.", evidence=["a"])
    brief = build(proposal["id"], target="Claude Code")
    assert "Add a retry to the exporter" in brief["prompt"]
    assert "risk level" in brief["prompt"], "the brief has to carry the real constraints"
    assert store.get_proposal(proposal["id"])["implementation_target"] == "Claude Code"
