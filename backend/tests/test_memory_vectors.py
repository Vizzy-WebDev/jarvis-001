"""Memory found by meaning (`memory/vectors.py`) — Phase 4.

Real parts: real SQLite and migrations, the real selection and prompt assembly, the real model layer
(config, discovery, the `openai_chat` driver) talking over HTTP to `stub_wire.StubWire`, whose
`/embeddings` answers come from a small CONCEPT embedder: words belonging to the same idea land on the
same axis, so "cook tonight" and "vegetarian, never suggest meat" are close while sharing no word at
all. That is the case keyword search provably cannot find — every test that claims a paraphrase is
found also shows the keyword path missing it.

The shipped `models/data/defaults.yaml` is replaced by a minimal one, so these tests run wherever the
suite does.
"""

from __future__ import annotations

import ast
import time
from pathlib import Path

import pytest

from jarvis import background, conversation
from jarvis.db import get_db, reset_for_tests as reset_db
from jarvis.memory import store, vectors
from jarvis.models import config, discovery, embeddings, engine, state
from jarvis.orchestrator.context import (
    MAX_RELEVANT, SMALL_MEMORY, RelevanceContext, select_memories,
)
from stub_wire import StubWire

# --- a model that understands a few ideas ----------------------------------------------------------

CONCEPTS = {
    "food": {"cook", "dinner", "supper", "tonight", "meal", "recipe", "vegetarian", "meat",
             "eat", "lunch", "breakfast", "hungry", "food"},
    "tech": {"database", "postgres", "sql", "server", "deploy", "code", "query"},
    "outdoors": {"walk", "walks", "beach", "hike", "hiking", "park", "trail", "run"},
    "travel": {"flight", "trip", "holiday", "hotel", "visit", "lagos", "passport"},
}
AXES = list(CONCEPTS)


def concept_vector(text: str) -> list[float]:
    words = {w.strip(".,!?;:—-'\"").lower() for w in text.split()}
    vector = [float(len(words & CONCEPTS[axis])) for axis in AXES]
    # A little of everything, so a text about nothing in particular still has a direction.
    return [v + 0.01 for v in vector]


class Provider:
    """A running stub provider, the config that points at it, and what was asked of it."""

    def __init__(self, tmp_path, **stub_kwargs):
        self.defaults = tmp_path / "defaults_min.yaml"
        self.defaults.write_text("version: 1\n", encoding="utf-8")
        self.stub = StubWire("openai_chat", models=[{"id": "chat-a"},
                                                    {"id": "embed-a", "type": "embedding"}],
                             embedder=stub_kwargs.pop("embedder", concept_vector), **stub_kwargs)
        self.stub.start()

    def connect(self, *, trust: str = "local", name: str = "stub", extra: dict | None = None) -> None:
        data = config.raw() if self.defaults else {}
        data.setdefault("version", 1)
        data.setdefault("connections", [])
        data["connections"].append({"name": name, "driver": "openai_chat",
                                    "base_url": self.stub.base_url, "trust": trust})
        data.update(extra or {})
        config.save(data)
        discovery.refresh()

    def asked(self) -> list[list[str]]:
        """The texts of every embedding request, in order."""
        return [r["body"]["input"] for r in self.stub.embeddings()]

    def queries(self) -> list[str]:
        """Only the single-text requests: a turn's own message, not an indexing batch or a probe."""
        return [a[0] for a in self.asked() if len(a) == 1 and a[0] != "hello"]


@pytest.fixture
def provider(scratch, tmp_path, monkeypatch):
    reset_db()
    conversation.reset_for_tests()
    state.reset()
    vectors.reset_for_tests()
    box: dict = {}

    def make(**kwargs) -> Provider:
        p = Provider(tmp_path, **kwargs)
        monkeypatch.setattr(config, "DEFAULTS_PATH", p.defaults)
        monkeypatch.setattr(config, "_defaults_cache", None)
        config.forget()
        box["p"] = p
        return p

    # Config must be read from the minimal defaults even for tests that never make a provider.
    minimal = tmp_path / "defaults_none.yaml"
    minimal.write_text("version: 1\n", encoding="utf-8")
    monkeypatch.setattr(config, "DEFAULTS_PATH", minimal)
    monkeypatch.setattr(config, "_defaults_cache", None)
    config.forget()
    yield make
    assert background.join_all(timeout=20)
    if "p" in box:
        box["p"].stub.stop()
    vectors.reset_for_tests()
    config.forget()
    state.reset()
    engine.router = engine.router
    reset_db()


def remember(text: str, **kw) -> dict:
    return store.create_memory(category=kw.pop("category", "Preferences"), text=text, **kw)


def fill(n: int, topic: str = "gardening") -> list[dict]:
    return [remember(f"An unrelated fact number {i} about {topic}.") for i in range(n)]


def assemble(text: str, **kw):
    return RelevanceContext().assemble(session_id="s1", text=text, **kw)


def indexed() -> int:
    return get_db().execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0]


def settle(timeout: float = 20) -> None:
    assert background.join_all(timeout=timeout), "background indexing never finished"


VEGETARIAN = "Vegetarian — never suggest meat."
QUESTION = "what should I cook tonight please"


def big_memory_with_the_fact(**kw) -> dict:
    fill(SMALL_MEMORY + 4)
    return remember(VEGETARIAN, **kw)


# --- the point of the phase ------------------------------------------------------------------------

def test_the_keyword_path_provably_misses_the_reworded_fact(provider):
    """The control: with no embedding model, a large memory leaves the fact out."""
    big_memory_with_the_fact()
    context = assemble(QUESTION)
    assert "Vegetarian" not in context.system
    assert "no embedding model" in context.notes["memory"]["strategy"]


def test_a_reworded_question_finds_the_fact_by_meaning(provider):
    p = provider()
    p.connect()
    big_memory_with_the_fact()

    first = assemble(QUESTION)           # builds the index in the background
    assert "still indexing" in first.notes["memory"]["strategy"]
    settle()
    second = assemble(QUESTION)

    assert "Vegetarian" in second.system, second.notes["memory"]
    assert "meaning" in second.notes["memory"]["strategy"]
    assert "number 3 about gardening" not in second.system
    assert QUESTION in p.queries()


def test_something_unrelated_is_not_pulled_in_by_meaning(provider):
    p = provider()
    p.connect()
    fill(SMALL_MEMORY + 4)
    remember("Loves long walks on the beach.")
    assemble(QUESTION)
    settle()
    context = assemble("which database should I deploy this to")
    assert "beach" not in context.system


def test_the_same_fact_is_found_whichever_words_the_person_uses(provider):
    p = provider()
    p.connect()
    big_memory_with_the_fact()
    assemble(QUESTION)
    settle()
    for phrasing in ("I am hungry what is for dinner", "any good recipe ideas for supper",
                     "plan a meal for the week please"):
        assert "Vegetarian" in assemble(phrasing).system, phrasing


# --- small memory: everything, and no call -----------------------------------------------------------

def test_a_small_memory_goes_in_whole_and_costs_no_embedding_call(provider):
    p = provider()
    p.connect()
    remember(VEGETARIAN)
    remember("Loves long walks on the beach.")
    context = assemble(QUESTION)
    assert "Vegetarian" in context.system and "beach" in context.system
    assert "memory is small" in context.notes["memory"]["strategy"]
    assert p.asked() == [], "a small memory must never pay for an embedding call"


def test_exactly_at_the_small_limit_is_still_small_and_one_over_is_not(provider):
    p = provider()
    p.connect()
    fill(SMALL_MEMORY - 1)
    remember(VEGETARIAN)
    assert "memory is small" in assemble(QUESTION).notes["memory"]["strategy"]
    remember("One more fact.")
    assert "memory is small" not in assemble(QUESTION).notes["memory"]["strategy"]


# --- never required: every way it can be unavailable ---------------------------------------------------

def test_no_embedding_model_leaves_selection_exactly_as_the_keyword_path_makes_it(provider):
    fill(SMALL_MEMORY + 10)
    remember("Uses Postgres at work.")
    available = store.list_memories()
    context = assemble("which database should I use for this")
    expected, _ = select_memories("which database should I use for this", available, None)
    assert [m["id"] for m in expected] == [
        m["id"] for m in available if m["text"] in context.system]
    assert "Postgres" in context.system


def test_a_provider_that_is_down_costs_one_failed_call_then_nothing(provider):
    p = provider(embed_status=503)
    p.connect()
    big_memory_with_the_fact()
    first = assemble(QUESTION)
    calls_after_first = len(p.asked())
    second = assemble("what is a good meal for tonight then")
    assert "Vegetarian" not in first.system and "Vegetarian" not in second.system
    assert "isn't answering" in second.notes["memory"]["strategy"]
    assert len(p.asked()) == calls_after_first, "the cool-down must stop further attempts"


def test_a_provider_that_goes_down_after_indexing_is_rested_not_retried_every_turn(provider):
    """The cool-down itself, not the "no model found" back-off: the space exists and every memory
    is indexed, THEN the provider stops answering. One turn pays for finding that out; the turns
    after it ask nothing at all until the cool-down ends, and still get keyword selection."""
    p = provider()
    p.connect()
    big_memory_with_the_fact()
    vectors.sync()
    assert indexed() == SMALL_MEMORY + 5
    p.stub.embed_status = 503

    first = assemble(QUESTION)
    assert "isn't answering" in first.notes["memory"]["strategy"]
    calls = len(p.asked())
    assert calls > 0
    for question in ("what is a good meal for tonight then", "any recipe ideas for dinner",
                     "what do I eat for lunch tomorrow"):
        later = assemble(question)
        assert "isn't answering" in later.notes["memory"]["strategy"]
    assert len(p.asked()) == calls, "a resting provider must not be asked again every turn"


def test_two_indexing_runs_at_once_embed_each_memory_only_once(provider):
    """Single-flight: a turn and the Memory screen (or two quick turns) can both start indexing.
    Only one run may do the work; the other must return at once rather than embed it all again."""
    import threading

    def slow(text: str) -> list[float]:
        time.sleep(0.05)
        return concept_vector(text)

    p = provider(embedder=slow)
    p.connect()
    fill(SMALL_MEMORY + 5)
    assert embeddings.ensure_space(vectors.SPACE, data_class=vectors.DATA_CLASS) is not None
    before = len(p.asked())

    results: list[int] = []
    threads = [threading.Thread(target=lambda: results.append(vectors.sync())) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    embedded = [text for batch in p.asked()[before:] for text in batch]
    assert sorted(results) == [0, 0, 0, SMALL_MEMORY + 5]
    assert len(embedded) == len(set(embedded)) == SMALL_MEMORY + 5


def test_a_slow_provider_never_holds_a_turn_up_beyond_the_deadline(provider, monkeypatch):
    def slow(text: str) -> list[float]:
        if text == QUESTION:  # only the turn's own message is slow; indexing is not what is timed
            time.sleep(1.5)
        return concept_vector(text)

    p = provider(embedder=slow)
    p.connect()
    big_memory_with_the_fact()
    monkeypatch.setattr(vectors, "QUERY_TIMEOUT_S", 0.2)
    vectors.sync()  # index (slow, but that is the background's problem) so only the query waits
    started = time.monotonic()
    context = assemble(QUESTION)
    assert time.monotonic() - started < 1.0
    assert "too slow" in context.notes["memory"]["strategy"]
    assert context.system  # the turn carries on, without the fact


def test_a_privacy_setting_that_forbids_it_means_keywords_and_says_why(provider):
    p = provider()
    p.connect(trust="standard", extra={"policies": {"data_classes": {"personal": ["local"]}}})
    big_memory_with_the_fact()
    context = assemble(QUESTION)
    assert "Vegetarian" not in context.system
    assert p.asked() == [], "nothing may be sent where the person's settings forbid it"
    status = vectors.status()
    assert status["state"] == "off" and "privacy settings" in status["reason"]


def test_a_very_short_message_is_not_worth_an_embedding(provider):
    p = provider()
    p.connect()
    big_memory_with_the_fact()
    assemble(QUESTION)
    settle()
    before = len(p.asked())
    context = assemble("yes please")
    assert len(p.asked()) == before
    assert "too short" in context.notes["memory"]["strategy"]


# --- a vector is valid only while everything that made it holds -----------------------------------------

def test_an_edited_memory_is_not_found_by_its_old_meaning(provider):
    p = provider()
    p.connect()
    memory = big_memory_with_the_fact()
    assemble(QUESTION)
    settle()
    assert "Vegetarian" in assemble(QUESTION).system

    store.update_memory(memory["id"], text="Enjoys loud music at the gym.")
    context = assemble("what should I cook for dinner tonight")
    assert "Vegetarian" not in context.system and "loud music" not in context.system
    settle()  # the edit is re-indexed in the background...
    again = assemble("what should I cook for dinner tonight")
    assert "loud music" not in again.system  # ...as what it NOW means (music, not food)


def test_a_switched_embedding_model_never_has_its_vectors_mixed_with_the_old(provider):
    p = provider()
    p.connect()
    big_memory_with_the_fact()
    assemble(QUESTION)
    settle()
    assert indexed() == SMALL_MEMORY + 5

    space = config.current().embedding_spaces["memory"]
    config.set_embedding_space("memory", primary=space.primary, dimension=space.dimension,
                               model_version="embed-b")
    vectors.reset_for_tests()
    context = assemble(QUESTION)
    assert "still indexing" in context.notes["memory"]["strategy"]
    assert "Vegetarian" not in context.system
    settle()
    assert "Vegetarian" in assemble(QUESTION).system


def test_a_vector_of_the_wrong_size_is_never_used(provider):
    p = provider()
    p.connect()
    memory = big_memory_with_the_fact()
    assemble(QUESTION)
    settle()
    get_db().execute("UPDATE memory_vectors SET vector = ? WHERE memory_id = ?",
                     (b"\x00\x00\x80\x3f", memory["id"]))
    context = assemble(QUESTION)
    assert "Vegetarian" not in context.system  # ranked by keyword only, not by a corrupt vector


def test_deleting_a_memory_takes_its_vector_with_it(provider):
    p = provider()
    p.connect()
    memory = big_memory_with_the_fact()
    assemble(QUESTION)
    settle()
    assert indexed() == SMALL_MEMORY + 5
    get_db().execute("DELETE FROM memories WHERE id = ?", (memory["id"],))
    assert indexed() == SMALL_MEMORY + 4


# --- what is never offered ----------------------------------------------------------------------------

def test_an_archived_or_contradicted_memory_is_never_offered(provider):
    p = provider()
    p.connect()
    old = big_memory_with_the_fact()
    assemble(QUESTION)
    settle()
    store.archive_memory(old["id"])
    assert "Vegetarian" not in assemble(QUESTION).system

    store.restore_memory(old["id"]) if hasattr(store, "restore_memory") else None
    again = remember("Likes cooking pasta for dinner.")
    store.create_candidate(source_kind="chat", category="Preferences", text="Eats meat now.",
                           conflict_with=again["id"], confidence=0.9)
    assemble(QUESTION)
    settle()
    assert "pasta" not in assemble(QUESTION).system


def test_the_important_floor_still_rides_whatever_the_question_is(provider):
    p = provider()
    p.connect()
    big_memory_with_the_fact()
    remember("Allergic to shellfish.", importance=4)
    assemble(QUESTION)
    settle()
    context = assemble("which database should I deploy this to")
    assert "shellfish" in context.system
    assert context.notes["memory"]["alwaysIncluded"] == 1


def test_no_more_than_the_relevance_cap_is_chosen_however_many_match(provider):
    p = provider()
    p.connect()
    for i in range(40):
        remember(f"Cooks dinner dish number {i} for supper.")
    assemble(QUESTION)
    settle()
    context = assemble(QUESTION)
    assert context.notes["memory"]["byRelevance"] <= MAX_RELEVANT
    assert context.notes["memory"]["byRelevance"] > 0


def test_a_tight_memory_budget_is_still_respected(provider):
    p = provider()
    p.connect()
    for i in range(40):
        remember(f"Cooks dinner dish number {i} for supper with a long wordy description. " * 3)
    assemble(QUESTION)
    settle()
    context = assemble(QUESTION, budget_tokens=500)
    notes = context.notes["memory"]
    assert notes["tokensUsed"] <= notes["tokenBudget"] == 100


# --- selection itself (pure) --------------------------------------------------------------------------

def mem(i: int, text: str, **kw) -> dict:
    return {"id": f"m{i}", "text": text, "category": kw.pop("category", "Preferences"),
            "importance": kw.pop("importance", None), **kw}


def many(n: int = SMALL_MEMORY + 5) -> list[dict]:
    return [mem(i, f"filler fact {i} about gardening") for i in range(n)]


def crowd(memories: list[dict], **special: float) -> dict[str, float]:
    """Every memory about equally (un)related, except the ones named."""
    return {m["id"]: special.get(m["id"], 0.05 + (i % 3) * 0.01) for i, m in enumerate(memories)}


def test_fusion_lets_meaning_and_words_each_bring_in_what_the_other_misses():
    memories = many() + [mem(100, "Vegetarian never suggest meat"),
                         mem(101, "Cooks every dinner on Sunday")]
    similarity = crowd(memories, m100=0.9, m101=0.1)
    chosen, notes = select_memories("what should I cook tonight", memories, None,
                                    similarity=similarity)
    ids = [m["id"] for m in chosen]
    assert "m100" in ids       # by meaning alone (no shared word)
    assert "m101" in ids       # by the word "cook(s)" alone — the keyword path still counts
    assert "meaning" in notes["strategy"]


def test_a_memory_without_a_vector_is_ranked_by_keyword_not_dropped():
    memories = many() + [mem(100, "Cooks every dinner on Sunday")]
    similarity = crowd(memories[:-1])  # the newest memory has no vector yet
    chosen, _ = select_memories("what should I cook tonight", memories, None,
                                similarity=similarity)
    assert "m100" in [m["id"] for m in chosen]


def test_importance_only_breaks_a_tie_between_equally_good_matches():
    memories = many() + [mem(100, "Vegetarian never suggest meat"),
                         mem(101, "Strict vegetarian diet", importance=3)]
    chosen, _ = select_memories("what should I cook tonight", memories, None,
                                similarity=crowd(memories, m100=0.9, m101=0.9))
    ids = [m["id"] for m in chosen]
    assert ids.index("m101") < ids.index("m100")


def test_a_better_match_is_not_outranked_by_importance():
    memories = many() + [mem(100, "Vegetarian never suggest meat"),
                         mem(101, "Strict vegetarian diet", importance=3)]
    chosen, _ = select_memories("what should I cook tonight", memories, None,
                                similarity=crowd(memories, m100=0.95, m101=0.6))
    ids = [m["id"] for m in chosen]
    assert ids.index("m100") < ids.index("m101")


def test_nothing_that_does_not_stand_out_from_the_rest_is_chosen_by_meaning():
    """Cosine values mean different things for different models, so "relevant" is "stands out"."""
    memories = many()
    chosen, _ = select_memories("what should I cook tonight", memories, None,
                                similarity=crowd(memories))
    assert chosen == [], "nothing stood out, so nothing is pulled in by meaning"


def test_when_every_memory_is_equally_close_nothing_stands_out():
    memories = many()
    chosen, _ = select_memories("what should I cook tonight", memories, None,
                                similarity={m["id"]: 0.3 for m in memories})
    assert chosen == []


def test_a_mixed_up_similarity_map_cannot_pull_in_a_memory_not_offered():
    chosen, _ = select_memories("anything at all here please", many(), None,
                                similarity={"ghost": 1.0})
    assert "ghost" not in [m["id"] for m in chosen]


# --- the space: finding a model, never approximating ---------------------------------------------------

def test_the_most_private_embedding_model_is_chosen_first(provider):
    p = provider()
    # Named so that alphabetical order would pick the hosted one: only trust can put "zz-home" first.
    p.connect(trust="standard", name="cloud")
    local = StubWire("openai_chat", models=[{"id": "embed-local", "type": "embedding"}],
                     embedder=concept_vector)
    local.start()
    try:
        data = config.raw()
        data["connections"].append({"name": "zz-home", "driver": "openai_chat",
                                    "base_url": local.base_url, "trust": "local"})
        config.save(data)
        discovery.refresh()
        assert [e for e, _ in embeddings.candidates("personal")] == [
            "zz-home/embed-local", "cloud/embed-a"]
        space = embeddings.ensure_space("memory", data_class="personal")
        assert space.primary == "zz-home/embed-local"
        assert space.dimension == len(AXES)
        # Written down once: asking again reads the file and calls nothing.
        calls = len(local.embeddings())
        assert embeddings.ensure_space("memory", data_class="personal").primary == space.primary
        assert len(local.embeddings()) == calls
    finally:
        local.stop()


def test_nothing_is_made_when_no_endpoint_says_it_embeds(provider):
    p = provider()
    p.stub.models = [{"id": "chat-a"}, {"id": "chat-b"}]
    p.connect()
    assert embeddings.ensure_space("memory", data_class="personal") is None
    assert config.current().embedding_spaces == {}


def test_a_space_whose_model_has_gone_is_not_quietly_re_pointed(provider):
    p = provider()
    p.connect()
    space = embeddings.ensure_space("memory", data_class="personal")
    other = StubWire("openai_chat", models=[{"id": "embed-other", "type": "embedding"}],
                     embedder=concept_vector)
    other.start()
    try:
        config.remove_model("stub", "embed-a") if False else None
        data = config.raw()
        data["connections"].append({"name": "other", "driver": "openai_chat",
                                    "base_url": other.base_url, "trust": "local"})
        config.save(data)
        discovery.refresh()
        assert embeddings.ensure_space("memory", data_class="personal").primary == space.primary
    finally:
        other.stop()


def test_a_dimension_that_is_not_the_spaces_is_refused(provider):
    p = provider(embedder=lambda t: [1.0, 2.0])
    p.connect()
    embeddings.ensure_space("memory", data_class="personal")
    p.stub.embedder = lambda t: [1.0, 2.0, 3.0]
    from jarvis.models.errors import InvalidRequest

    with pytest.raises(InvalidRequest):
        embeddings.embed("memory", ["a"], data_class="personal")


def test_every_embedding_call_declares_memory_as_personal_data(provider, monkeypatch):
    p = provider()
    p.connect()
    big_memory_with_the_fact()
    seen: list[str] = []
    real_embed = embeddings.embed

    def spy(space, inputs, *, data_class):
        seen.append(data_class)
        return real_embed(space, inputs, data_class=data_class)

    monkeypatch.setattr("jarvis.models.embed", spy)
    monkeypatch.setattr(embeddings, "embed", spy)
    assemble(QUESTION)
    settle()
    assemble(QUESTION)
    assert seen and set(seen) == {"personal"}


# --- indexing -------------------------------------------------------------------------------------------

def test_missing_vectors_are_built_once_in_one_batch_in_the_background(provider):
    p = provider()
    p.connect()
    big_memory_with_the_fact()
    n = SMALL_MEMORY + 5
    assemble(QUESTION)
    settle()
    batches = [a for a in p.asked() if len(a) > 1]
    assert len(batches) == 1 and len(batches[0]) == n, [len(a) for a in p.asked()]
    assert indexed() == n
    # Asking again, or racing several turns, never embeds a memory twice.
    assemble(QUESTION)
    assemble("what is a good meal to eat tonight")
    settle()
    assert [len(a) for a in p.asked() if len(a) > 1] == [n]


def test_a_failed_index_pauses_and_a_working_provider_later_completes_it(provider):
    p = provider(embed_status=503)
    p.connect()
    big_memory_with_the_fact()
    assert vectors.sync() == 0 and indexed() == 0
    attempts = len(p.asked())
    assert vectors.sync() == 0
    assert len(p.asked()) == attempts, "a provider that is down must not be hammered"
    p.stub.embed_status = None
    vectors.reset_for_tests()
    assert vectors.sync() == SMALL_MEMORY + 5


def test_a_repeated_question_is_embedded_only_once(provider):
    p = provider()
    p.connect()
    big_memory_with_the_fact()
    assemble(QUESTION)
    settle()
    assemble(QUESTION)
    assemble(QUESTION)
    assemble(QUESTION)
    assert p.queries().count(QUESTION) == 1


def test_indexing_never_raises_into_a_turn_even_when_everything_is_broken(provider, monkeypatch):
    p = provider()
    p.connect()
    big_memory_with_the_fact()
    monkeypatch.setattr(vectors, "_embed", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert assemble(QUESTION).system
    settle()
    assert indexed() == 0


# --- what the person is told ----------------------------------------------------------------------------

def test_the_status_says_each_state_in_plain_words_and_never_calls_a_model(provider):
    off = vectors.status()
    assert off["state"] == "off" and "Add an embedding model" in off["reason"]

    p = provider()
    p.connect()
    ready = vectors.status()
    assert ready["state"] == "ready" and ready["model"] == "stub/embed-a"

    big_memory_with_the_fact()
    assert p.asked() == []
    embeddings.ensure_space("memory", data_class="personal")
    probe_calls = len(p.asked())
    building = vectors.status()
    assert building["state"] == "building" and building["indexed"] == 0
    assert building["total"] == SMALL_MEMORY + 5
    vectors.sync()
    on = vectors.status()
    assert on["state"] == "on" and on["indexed"] == on["total"] == SMALL_MEMORY + 5
    assert len([a for a in p.asked() if len(a) == 1]) == probe_calls  # status itself asked nothing


def test_the_status_route_reports_it(provider):
    from starlette.testclient import TestClient

    from jarvis.main import create_app

    p = provider()
    p.connect()
    client = TestClient(create_app())
    body = client.get("/api/memories/search-status").json()
    assert body["state"] == "ready" and body["total"] == 0
    assert body["reason"].startswith("Not needed yet")

    # Found by the scratch boot: a memory already past the small limit, not indexed yet, was told
    # "once you have more memories" — untrue. It must say the search starts on the next message.
    fill(SMALL_MEMORY + 1)
    body = client.get("/api/memories/search-status").json()
    assert body["state"] == "ready" and body["total"] == SMALL_MEMORY + 1
    assert body["reason"] == "It switches on by itself with your next message."


# --- structure ----------------------------------------------------------------------------------------

def test_the_migration_made_the_table_and_the_database_is_at_version_38(provider):
    assert get_db().execute("PRAGMA user_version").fetchone()[0] >= 38
    columns = {r[1] for r in get_db().execute("PRAGMA table_info(memory_vectors)").fetchall()}
    assert {"memory_id", "space", "model_version", "dimension", "text_hash", "vector"} <= columns


def test_the_memory_store_stays_a_leaf_and_vectors_load_the_model_layer_only_when_used():
    package = Path(__file__).resolve().parent.parent / "jarvis"

    def top_level_imports(path: Path) -> set[str]:
        found: set[str] = set()
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.ImportFrom):
                found.add(("." * node.level) + (node.module or ""))
            elif isinstance(node, ast.Import):
                found.update(a.name for a in node.names)
        return found

    assert not any("models" in name for name in top_level_imports(package / "memory" / "vectors.py"))
    assert not any("numpy" in name for name in top_level_imports(package / "memory" / "vectors.py"))
    store_imports = top_level_imports(package / "memory" / "store.py")
    assert not any(n.startswith("..models") or "vectors" in n for n in store_imports)
