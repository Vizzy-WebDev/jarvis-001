"""The self-model (§45): grounded, or it says nothing.

The rule under test throughout is that a claim about Jarvis is backed by a
counter, a row or a live policy value — never by an estimate. So most of these
check what it REFUSES to say: no ratio from four attempts, no verdict on prose,
no dimensions built that nobody asked for.
"""

from __future__ import annotations

import pytest

from jarvis.db import reset_for_tests as reset_db
from jarvis.improvement import store as improvement_store
from jarvis.self import model, signals, store, verify


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    yield
    reset_db()


# --- signals: when is the self-model worth consulting ------------------------

def test_nothing_happening_fires_nothing():
    assert signals.any_fired(signals.detect()) is False


def test_a_pending_decision_is_an_authority_signal():
    fired = signals.detect(active_jobs=[{"status": "awaiting_decision"}])
    assert fired["authority"] is True and signals.any_fired(fired)


def test_a_lesson_matching_what_this_turn_is_doing_fires():
    fired = signals.detect(scopes_in_play=["tool:run_code"],
                           active_lesson_scopes=["tool:run_code", "general"])
    assert fired["knownFailure"] is True and fired["matchedScopes"] == ["tool:run_code"]


def test_an_unrelated_lesson_does_not_fire():
    fired = signals.detect(scopes_in_play=["tool:get_time"],
                           active_lesson_scopes=["tool:run_code"])
    assert fired["knownFailure"] is False and signals.any_fired(fired) is False


def test_never_having_done_something_fires():
    fired = signals.detect(no_track_record_checks=[{"axis": "tool", "key": "x", "attempts": 0}])
    assert fired["noTrackRecord"] is True


def test_a_clean_track_record_never_fires():
    """Only genuine prior trouble is worth prompt space."""
    clean = {**signals.detect(), "toolReliability": [{"key": "run_code", "failures": 0}]}
    assert signals.any_fired(clean) is False
    troubled = {**signals.detect(), "toolReliability": [{"key": "run_code", "failures": 2}]}
    assert signals.any_fired(troubled) is True


# --- reliability: the refusal that matters -----------------------------------

def test_four_attempts_is_not_a_track_record_and_five_is():
    for _ in range(model.MIN_ATTEMPTS_FOR_RATIO - 1):
        store.record_attempt("tool", "run_code", True)
    assert model.reliability_of("tool", "run_code")["verdict"] == model.NO_TRACK_RECORD

    store.record_attempt("tool", "run_code", False)
    measured = model.reliability_of("tool", "run_code")
    assert measured["verdict"] == "measured"
    assert measured["attempts"] == 5 and measured["failures"] == 1
    assert measured["successRate"] == 0.8


def test_something_never_used_says_so_rather_than_claiming_perfection():
    verdict = model.reliability_of("tool", "never_touched")
    assert verdict["verdict"] == model.NO_TRACK_RECORD
    assert verdict["attempts"] == 0
    assert "successRate" not in verdict


def test_a_broken_recorder_is_distinguishable_from_never_having_been_used():
    """Without this, both read as an empty tally and mean completely different
    things."""
    healthy = model.build(["can_do"], about=["x"])["can_do"]["recorder"]
    assert healthy["healthy"] is True

    store.record_capture_health(source="self", name="tool:x", ok=False,
                                error_message="disk full")
    broken = model.build(["can_do"], about=["x"])["can_do"]["recorder"]
    assert broken["healthy"] is False
    assert "may not mean the capability was never used" in broken["note"]


# --- assembling --------------------------------------------------------------

def test_asking_for_nothing_builds_nothing():
    """No default "everything": assembling every dimension costs real reads, and
    a caller that wanted one should not silently pay for five."""
    assert model.build() == {}
    assert model.build([]) == {}


def test_an_unknown_dimension_is_skipped_rather_than_crashing():
    assert model.build(["not_a_dimension", "whats_its_call"]).keys() == {"whats_its_call"}


def test_the_floors_quote_the_live_numbers_rather_than_describing_them():
    """The prose and the value are one read, so they cannot drift apart."""
    from jarvis.prefs import set_prefs

    set_prefs({"memoryTrust": "ask", "improvementTrust": "balanced"})
    floors = model.build(["whats_its_call"])["whats_its_call"]["hardFloors"]
    assert "Nothing about the user is saved without asking" in floors[0]
    assert "at least 2 separate pieces of evidence" in floors[1]

    set_prefs({"memoryTrust": "balanced", "improvementTrust": "ask"})
    floors = model.build(["whats_its_call"])["whats_its_call"]["hardFloors"]
    assert "0.85 confidence" in floors[0]
    assert "No change to how I work applies itself" in floors[1]


def test_how_it_fails_is_a_view_over_improvement_not_a_second_store():
    improvement_store.create_lesson(text="run_code needs a timeout", scope="tool:run_code",
                                    evidence=["out_1"])
    lessons = model.build(["failure_modes"])["failure_modes"]["lessons"]
    assert lessons[0]["text"] == "run_code needs a timeout"


# --- citations: the one checkable slice --------------------------------------

def test_zero_does_not_match_inside_one_hundred():
    """The bug this exists not to reproduce: substring matching made a citable 0
    match inside "100%"."""
    assert verify.mentions_number("that worked 100% of the time", "0") is False
    assert verify.mentions_number("it failed 0 times", "0") is True
    assert verify.mentions_number("version 1.75 shipped", "1") is False


def test_a_number_used_in_the_reply_verifies_and_one_ignored_does_not():
    snapshot_id = store.save_snapshot(conversation_id="c1",
                                      snapshot={"can_do": {"attempts": 7, "failures": 2}})
    used = verify.verify_citation(snapshot_id, "can_do.attempts", "I've tried that 7 times.")
    assert used["verdict"] == verify.USED

    ignored = verify.verify_citation(snapshot_id, "can_do.failures", "It usually works.")
    assert ignored["verdict"] == verify.IGNORED


def test_prose_is_unverifiable_and_never_guessed_at():
    """Whether a sentence's MEANING matches its source is not solvable this way,
    and pretending otherwise would be the false confidence this exists to avoid."""
    snapshot_id = store.save_snapshot(conversation_id="c1",
                                      snapshot={"note": "it has been reliable"})
    result = verify.verify_citation(snapshot_id, "note", "It has been reliable.")
    assert result["verdict"] == verify.UNVERIFIABLE


def test_only_numbers_are_logged_as_citable_at_all():
    snapshot_id = store.save_snapshot(
        conversation_id="c1", snapshot={"attempts": 7, "note": "words", "ok": True})
    fields = {c["field_name"] for c in store.list_citations(snapshot_id)}
    assert fields == {"attempts"}


# --- goals -------------------------------------------------------------------

def test_a_goal_keeps_the_user_s_own_words_alongside_the_paraphrase():
    """So drift is later judged against what was actually said, not against a
    paraphrase of it."""
    recorded = store.declare_goal(scope_kind="conversation", scope_ref="s1",
                                  goal_text="get the export working",
                                  source_turn_text="the export keeps failing, sort it out")
    assert recorded["source_turn_text"] == "the export keeps failing, sort it out"
    assert store.get_active_goal("conversation", "s1")["id"] == recorded["id"]


def test_the_goal_is_reported_as_a_reading_not_a_verified_account():
    store.declare_goal(scope_kind="conversation", scope_ref="s1", goal_text="g",
                       source_turn_text="their words")
    doing = model.build(["doing_now"], session_id="s1")["doing_now"]
    assert "not a verified account" in doing["declaredGoal"]["note"]
