"""The context assembler (§23): relevance, a real budget, and honest accounting.

The property under test is not "memory appears in the prompt" — the original did
that by injecting everything. It is that what appears was CHOSEN, that the choice
is explainable, and that the budget is never silently exceeded.
"""

from __future__ import annotations

import pytest

from jarvis import conversation
from jarvis.db import reset_for_tests as reset_db
from jarvis.memory import store
from jarvis.orchestrator.context import (
    ALWAYS_INCLUDE_IMPORTANCE, RelevanceContext, estimate_tokens, select_memories, trim_messages,
)
from jarvis.prompt_format import CACHE_BREAK


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    conversation.reset_for_tests()
    yield
    conversation.reset_for_tests()
    reset_db()


def remember(text, **kw):
    return store.create_memory(category=kw.pop("category", "About You"), text=text, **kw)


# --- relevance ---------------------------------------------------------------

def test_an_unrelated_memory_is_left_out():
    """The whole point of the change: everything-every-turn stops scaling exactly
    when memory starts being worth having."""
    remember("Uses Postgres at work.")
    remember("Enjoys long walks on the beach.")
    context = RelevanceContext().assemble(session_id="s1", text="which database should I use")
    assert "Postgres" in context.system
    assert "long walks" not in context.system


def test_a_word_form_difference_does_not_lose_the_relevant_fact():
    """"...should I USE" against a memory saying "USES Postgres" shares no term
    at all without the suffix trim, which is a silly way to miss the one fact
    that mattered."""
    remember("Uses Postgres at work.")
    assert "Postgres" in RelevanceContext().assemble(
        session_id="s1", text="what should I use").system


def test_importance_overrides_relevance_and_never_the_other_way_round():
    """A fact marked as changing how they should be helped must not drop out
    because this sentence happens to share no words with it."""
    remember("Allergic to shellfish.", importance=ALWAYS_INCLUDE_IMPORTANCE)
    context = RelevanceContext().assemble(session_id="s1", text="what's a good pasta recipe")
    assert "shellfish" in context.system
    assert context.notes["memory"]["alwaysIncluded"] == 1


def test_the_selection_explains_itself():
    for i in range(5):
        remember(f"An unrelated fact number {i}.")
    notes = RelevanceContext().assemble(session_id="s1", text="tell me about ferrets"
                                        ).notes["memory"]
    assert notes["considered"] == 5 and notes["dropped"] == 5
    assert "no embeddings" in notes["strategy"]


def test_a_contradicted_memory_is_not_offered_for_selection_at_all():
    memory = remember("Uses a Mac.")
    store.create_candidate(source_kind="chat", category="About You",
                           text="Uses a Windows PC.", conflict_with=memory["id"], confidence=0.9)
    assert "Mac" not in RelevanceContext().assemble(session_id="s1", text="what do I use").system


# --- the budget --------------------------------------------------------------

def test_memory_cannot_take_more_than_its_share_of_the_budget():
    for i in range(200):
        remember(f"Fact {i}: " + "some fairly wordy remembered detail " * 5)
    context = RelevanceContext(budget_tokens=2000, memory_share=0.2).assemble(
        session_id="s1", text="tell me a remembered detail")
    used = context.notes["memory"]["tokensUsed"]
    assert used <= context.notes["memory"]["tokenBudget"] == 400


def test_the_transcript_is_trimmed_to_what_is_left():
    for i in range(100):
        conversation.push_user_text("s1", f"message number {i} " + "padding " * 40)
    context = RelevanceContext(budget_tokens=1500).assemble(session_id="s1", text="carry on")
    assert context.notes["messagesKept"] < context.notes["messagesAvailable"]
    assert context.messages[-1]["text"].startswith("message number 99")


def test_trimming_never_orphans_a_tool_result():
    """Every provider rejects a transcript whose first message is a tool result
    with no assistant call above it."""
    messages = [
        {"role": "assistant", "toolCalls": [{"id": "1", "name": "t", "args": {}}]},
        {"role": "tool", "toolResults": [{"id": "1", "name": "t", "result": "x"}]},
        {"role": "user", "text": "and now?"},
    ]
    assert trim_messages(messages, budget_tokens=1)[0]["role"] != "tool"


def test_the_estimate_says_it_is_an_estimate():
    notes = RelevanceContext().assemble(session_id="s1", text="hello").notes
    assert "estimate" in notes["estimate"].lower()
    assert estimate_tokens("a" * 400) == 100


# --- the prompt shape --------------------------------------------------------

def test_per_turn_facts_sit_after_the_cache_breakpoint():
    """Anthropic's caching keys on an exact prefix, so a per-turn fact on the
    wrong side of this costs the cache on every single turn."""
    remember("Drinks tea.")
    system = RelevanceContext().assemble(session_id="s1", text="what do I drink").system
    stable, _, volatile = system.partition(CACHE_BREAK)
    assert "You are Jarvis" in stable
    assert "Drinks tea." in volatile
    assert "Right now it is" in volatile


def test_a_low_confidence_turn_says_so_to_the_model_only_when_it_applies():
    plain = RelevanceContext().assemble(session_id="s1", text="delete it")
    unsure = RelevanceContext().assemble(session_id="s1", text="delete it", low_confidence=True)
    assert "misheard" not in plain.system
    assert "misheard" in unsure.system


def test_no_memories_means_no_empty_memory_heading():
    system = RelevanceContext().assemble(session_id="s1", text="hello").system
    assert "What you remember about the user" not in system
