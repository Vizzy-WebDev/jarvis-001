"""Memory (§22): the store, the policy floors, and the checkpoint engine.

The policy is exercised as an exhaustive truth table — it is a pure function, so
there is no excuse for testing it any other way, and its floors are the thing
most worth never letting drift.

The checkpoint engine is driven through the REAL extraction path against a stub
model, so what is verified is the actual prompt-to-candidate-to-memory flow
including the deterministic duplicate backstop, not a description of it.
"""

from __future__ import annotations

import json

import pytest

from jarvis import chat_store, conversation
from jarvis.db import reset_for_tests as reset_db
from jarvis.events import EventType
from jarvis.events.bus import EventBus
from jarvis.memory import review, store
from jarvis.memory.policy import AUTO_APPROVE, REQUIRE_APPROVAL, THRESHOLDS, decide


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    conversation.reset_for_tests()
    yield
    conversation.reset_for_tests()
    reset_db()


# --- the policy, as a truth table --------------------------------------------

@pytest.mark.parametrize("trust", list(THRESHOLDS))
@pytest.mark.parametrize("confidence", [None, "high", 0.0, 0.5, 0.84, 0.85, 1.0, 5.0])
def test_a_conflict_always_needs_a_human(trust, confidence):
    """No trust level, and no confidence score, may ever auto-resolve a
    conflict: resolving one changes or duplicates something that already
    exists."""
    assert decide({"confidence": confidence, "conflictsWithId": "mem_1"}, trust) == REQUIRE_APPROVAL


@pytest.mark.parametrize("trust", list(THRESHOLDS))
def test_an_unscorable_candidate_never_auto_saves(trust):
    """A caller that cannot say how sure it is does not get the benefit of the
    doubt."""
    for confidence in (None, "very", float("nan"), float("inf"), True):
        assert decide({"confidence": confidence}, trust) == REQUIRE_APPROVAL


def test_ask_is_infinity_not_merely_a_high_number():
    """That is what makes the default reproduce approval-first behaviour for
    every score a model could return, including a buggy one above 1.0."""
    assert THRESHOLDS["ask"] == float("inf")
    assert decide({"confidence": 5.0}, "ask") == REQUIRE_APPROVAL


@pytest.mark.parametrize("confidence,expected", [
    (0.84, REQUIRE_APPROVAL), (0.85, AUTO_APPROVE), (1.0, AUTO_APPROVE)])
def test_balanced_uses_its_threshold(confidence, expected):
    assert decide({"confidence": confidence}, "balanced") == expected


def test_auto_still_requires_a_real_score():
    assert decide({"confidence": 0.0}, "auto") == AUTO_APPROVE
    assert decide({}, "auto") == REQUIRE_APPROVAL


# --- the store ---------------------------------------------------------------

def test_an_edit_keeps_the_previous_state_as_history():
    memory = store.create_memory(category="About You", text="Drinks coffee.")
    store.update_memory(memory["id"], text="Drinks tea.", reason="They corrected me.")
    history = store.version_history(memory["id"])
    # Version rows record PAST states, so both hold the pre-edit text: one from
    # creation, one written just before the overwrite. What matters is that the
    # superseded text is recoverable, with the reason it changed.
    assert [(v["text"], v["reason"]) for v in history] == [
        ("Drinks coffee.", "They corrected me."), ("Drinks coffee.", "Created.")]
    assert store.get_memory(memory["id"])["text"] == "Drinks tea."


def test_a_contradicted_memory_stops_being_asserted_while_the_conflict_waits():
    """The other half of "a conflict always needs a human": while it waits, the
    OLD, possibly-wrong memory must not keep being stated as settled fact."""
    memory = store.create_memory(category="About You", text="Uses a Mac.")
    assert "Uses a Mac." in store.approved_memories_text()

    store.create_candidate(source_kind="chat", category="About You",
                           text="Uses a Windows PC.", conflict_with=memory["id"], confidence=0.9)
    assert "Uses a Mac." not in store.approved_memories_text()


def test_an_expired_memory_is_not_asserted_but_is_not_destroyed():
    store.create_memory(category="Travel", text="In Lisbon this week.",
                        expires_at="2000-01-01T00:00:00.000Z")
    assert store.approved_memories_text() == ""
    assert len(store.list_memories(include_expired=True)) == 1


def test_importance_is_null_until_something_actually_judges_it():
    """A default of 3 would be a number nobody chose, read later as if someone
    had."""
    assert store.create_memory(category="About You", text="Likes tea.")["importance"] is None


def test_the_injected_set_is_capped_however_large_the_store_grows():
    for i in range(store.MAX_INJECTED_MEMORIES + 10):
        store.create_memory(category="About You", text=f"Fact number {i}.")
    lines = [l for l in store.approved_memories_text().splitlines() if l.startswith("- ")]
    assert len(lines) == store.MAX_INJECTED_MEMORIES


def test_a_category_a_model_invents_is_not_permanent_until_something_lands_in_it():
    store.propose_category("Hobbies")
    assert [c for c in store.list_categories() if c["name"] == "Hobbies"][0]["status"] == "pending"
    assert "Hobbies" not in [c["name"] for c in store.list_categories(include_pending=False)]


def test_resolving_a_conflict_with_the_new_fact_edits_rather_than_duplicates():
    """Two memories asserting opposite things is the state this prevents."""
    old = store.create_memory(category="About You", text="Uses a Mac.")
    candidate = store.create_candidate(source_kind="chat", category="About You",
                                       text="Uses a Windows PC.", conflict_with=old["id"],
                                       confidence=0.9)
    store.resolve_conflict(candidate["id"], "use-new")
    remaining = store.list_memories()
    assert len(remaining) == 1 and remaining[0]["text"] == "Uses a Windows PC."


def test_an_unreviewed_draft_dies_with_its_conversation_but_a_memory_does_not():
    """Enforced by the schema (a real, permanent delete), not by remembering."""
    convo = chat_store.create_conversation()
    store.create_candidate(conversation_id=convo["id"], source_kind="chat",
                           category="About You", text="A draft.", confidence=0.2)
    memory = store.create_memory(category="About You", text="An approved fact.")

    # "Delete" moves the conversation to the recycle bin — reversible, so its
    # candidate is suppressed rather than gone.
    chat_store.delete_conversation(convo["id"])
    assert store.list_pending_candidates() == []
    assert store.get_memory(memory["id"]) is not None

    # Restoring brings it back into review with no extra step needed.
    chat_store.restore_conversation(convo["id"])
    assert len(store.list_pending_candidates()) == 1

    # Only a real, permanent delete removes the row for good, via the
    # existing ON DELETE CASCADE.
    chat_store.delete_conversation(convo["id"])
    assert chat_store.purge_conversation(convo["id"]) is True
    assert store.list_pending_candidates() == []


# --- the checkpoint engine ---------------------------------------------------


def extracts(*candidates):
    return json.dumps({"candidates": list(candidates)})


