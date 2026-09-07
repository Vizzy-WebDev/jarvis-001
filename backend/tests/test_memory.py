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
from jarvis.gateway import availability, connections, registry
from jarvis.memory import review, store
from jarvis.memory.policy import AUTO_APPROVE, REQUIRE_APPROVAL, THRESHOLDS, decide

from stub_openai_server import StubModelServer


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    conversation.reset_for_tests()
    availability.reset_for_tests()
    yield
    availability.reset_for_tests()
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
    """Enforced by the schema, not by remembering."""
    convo = chat_store.create_conversation()
    store.create_candidate(conversation_id=convo["id"], source_kind="chat",
                           category="About You", text="A draft.", confidence=0.2)
    memory = store.create_memory(category="About You", text="An approved fact.")

    chat_store.delete_conversation(convo["id"])
    assert store.list_pending_candidates() == []
    assert store.get_memory(memory["id"]) is not None


# --- the checkpoint engine ---------------------------------------------------

@pytest.fixture
def stub():
    server = StubModelServer()
    server.base_url = server.start()
    conn = connections.add_connection(adapter="openai-compatible", base_url=server.base_url,
                                      label="stub", provider="custom", kind="local",
                                      key_required=False)
    registry.add_model(connection_id=conn["id"], model="stub-model")
    yield server
    server.stop()


def extracts(*candidates):
    return json.dumps({"candidates": list(candidates)})


def test_a_high_confidence_fact_auto_saves_only_at_a_trusting_setting(stub, monkeypatch):
    from jarvis import prefs

    stub.says(extracts({"text": "Drinks green tea.", "category": "About You",
                        "confidence": 0.95, "importance": 2}))
    prefs.set_prefs({"memoryTrust": "balanced"})
    result = review.checkpoint_from_text("I only drink green tea.", source_kind="chat")

    assert [m["text"] for m in result["autoSaved"]] == ["Drinks green tea."]
    saved = store.list_memories()[0]
    assert saved["origin"] == "auto", "how consent was given must be recorded"
    assert saved["importance"] == 2


def test_the_default_setting_asks_rather_than_saves(stub):
    stub.says(extracts({"text": "Drinks green tea.", "category": "About You",
                        "confidence": 0.99}))
    result = review.checkpoint_from_text("I only drink green tea.", source_kind="chat")
    assert result["autoSaved"] == []
    assert [c["text"] for c in result["candidates"]] == ["Drinks green tea."]
    assert store.list_memories() == []


def test_a_reworded_repeat_of_a_saved_fact_is_dropped_deterministically(stub):
    """The live failure this backstop exists for: Jarvis recalls a saved fact
    out loud, the next checkpoint extracts its own recall as new evidence, and
    it lands as a conflict against the very memory it duplicates."""
    store.create_memory(category="About You", text="Sister is getting married in March")
    stub.says(extracts({"text": "User's sister is getting married in March.",
                        "category": "About You", "confidence": 0.95}))

    result = review.checkpoint_from_text("my sister's wedding is in March", source_kind="chat")
    assert result["candidates"] == [] and result["autoSaved"] == []
    assert len(store.list_memories()) == 1


def test_a_genuine_change_is_still_proposed(stub):
    store.create_memory(category="About You", text="Uses a Mac.")
    stub.says(extracts({"text": "Uses a Windows PC.", "category": "About You",
                        "confidence": 0.95, "conflictsWithId": "nope"}))
    result = review.checkpoint_from_text("I switched to Windows", source_kind="chat")
    assert [c["text"] for c in result["candidates"]] == ["Uses a Windows PC."]


def test_nothing_worth_remembering_is_a_normal_answer(stub):
    stub.says(extracts())
    assert review.checkpoint_from_text("what's the weather", source_kind="chat") == {
        "candidates": [], "autoSaved": []}


def test_a_conversation_checkpoint_only_reads_what_is_new(stub):
    convo = chat_store.create_conversation()
    chat_store.append_message(convo["id"], {"role": "user", "text": "I live in Lagos."})
    stub.says(extracts({"text": "Lives in Lagos.", "category": "About You", "confidence": 0.5}))
    review.checkpoint_conversation(convo["id"], "new chat")
    assert len(stub.requests) == 1

    # Nothing new since: no second model call, and no duplicate candidate.
    review.checkpoint_conversation(convo["id"], "new chat")
    assert len(stub.requests) == 1
    assert len(store.list_pending_candidates()) == 1


def test_no_model_available_defers_rather_than_losing_the_material(stub):
    """A failed checkpoint must not silently drop what it was about to read."""
    convo = chat_store.create_conversation()
    chat_store.append_message(convo["id"], {"role": "user", "text": "I live in Lagos."})
    stub.fails(429, "Rate limit exceeded")

    result = review.checkpoint_conversation(convo["id"], "new chat")
    assert result.get("skipped") is True
    assert store.get_checkpoint(convo["id"]) == 0, "the pointer must not advance"


def test_an_auto_save_is_announced_so_it_can_be_undone(stub):
    from jarvis import prefs

    seen = []
    ebus = EventBus()
    ebus.subscribe(EventType.NOTIFICATION_CREATED, lambda e: seen.append(e.payload))
    prefs.set_prefs({"memoryTrust": "auto"})
    stub.says(extracts({"text": "Drinks green tea.", "category": "About You", "confidence": 0.4}))

    review.checkpoint_from_text("green tea only", source_kind="chat", event_bus=ebus)
    assert seen and "remembered" in seen[0]["title"]
