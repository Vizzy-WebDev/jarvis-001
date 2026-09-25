"""Memory and the profile notes over HTTP.

`memory/store.py` is tested on its own; what is worth testing here is what a
screen depends on and a route could quietly break — the shape of an empty
install, that a conflict cannot be resolved by approving it, that deleting is
two steps, and that the profile notes really are one category of the same store
rather than a second place things live.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from jarvis.memory import store


@pytest.fixture
def client(scratch):
    from jarvis.main import create_app

    return TestClient(create_app())


def test_an_empty_install_answers_the_recorded_shape(client):
    assert client.get("/api/memories").json() == {"memories": [], "conflicted": []}
    assert client.get("/api/memories/candidates").json() == {"candidates": []}
    assert client.get("/api/profile").json() == {"entries": []}
    # The categories are seeded by the migration, not invented by the route.
    names = [c["name"] for c in client.get("/api/memories/categories").json()["categories"]]
    assert "About You" in names and "Uncategorized" in names


def test_the_candidate_and_category_paths_are_not_swallowed_by_the_id_route(client):
    """Declaration order is load-bearing: a path parameter declared first would
    match both of these and answer "no such memory" for a route that exists."""
    assert client.get("/api/memories/candidates").status_code == 200
    assert client.get("/api/memories/categories").status_code == 200


def test_writing_by_hand_records_that_a_person_asked_for_it(client):
    created = client.post("/api/memories",
                          json={"text": "Drinks tea, never coffee",
                                "category": "Preferences"}).json()
    assert created["ok"] is True
    # Typed by the person it is about: that IS the consent a review exists to
    # obtain, which is what `explicit` records — not `approved`, which means a
    # candidate was reviewed.
    assert created["memory"]["origin"] == "explicit"
    assert client.get("/api/memories").json()["memories"][0]["text"] == "Drinks tea, never coffee"


def test_an_edit_keeps_the_previous_text_as_a_version(client):
    memory = client.post("/api/memories", json={"text": "Lives in Lagos"}).json()["memory"]
    client.patch(f"/api/memories/{memory['id']}", json={"text": "Lives in Abuja"})

    # Two rows, newest first: the pre-edit state, and the "Created." row the
    # store writes up front so history starts at the beginning rather than at
    # the first edit.
    versions = client.get(f"/api/memories/{memory['id']}/versions").json()["versions"]
    assert [v["text"] for v in versions] == ["Lives in Lagos", "Lives in Lagos"]
    assert [v["reason"] for v in versions][-1] == "Created."
    assert client.get("/api/memories").json()["memories"][0]["text"] == "Lives in Abuja"


def test_versions_of_something_that_never_existed_is_empty_not_a_404(client):
    """"What changed about this?" has a true answer for an unknown id: nothing
    did. The recording agrees, and a 404 here would make a screen show an error
    for a question that was answered."""
    assert client.get("/api/memories/nope/versions").json() == {"versions": []}


def test_archiving_hides_it_and_restoring_brings_it_back(client):
    memory = client.post("/api/memories", json={"text": "Allergic to shellfish"}).json()["memory"]
    client.post(f"/api/memories/{memory['id']}/archive")
    assert client.get("/api/memories").json()["memories"] == []
    assert len(client.get("/api/memories?includeArchived=true").json()["memories"]) == 1

    client.post(f"/api/memories/{memory['id']}/restore")
    assert len(client.get("/api/memories").json()["memories"]) == 1


def test_merging_keeps_the_primary_and_archives_the_rest(client):
    first = client.post("/api/memories", json={"text": "Has a dog"}).json()["memory"]
    second = client.post("/api/memories", json={"text": "Owns a dog called Rex"}).json()["memory"]

    merged = client.post("/api/memories/merge", json={
        "primaryId": first["id"], "otherIds": [second["id"]],
        "text": "Has a dog called Rex"}).json()
    assert merged["ok"] is True

    live = client.get("/api/memories").json()["memories"]
    assert [m["text"] for m in live] == ["Has a dog called Rex"]
    # Archived, not deleted: a merge that turns out to be wrong is recoverable.
    assert len(client.get("/api/memories?includeArchived=true").json()["memories"]) == 2


def test_a_merge_with_nothing_to_merge_into_is_refused(client):
    assert client.post("/api/memories/merge", json={"text": "x"}).status_code == 400


def test_approving_a_candidate_saves_it_with_the_edits_made_while_reading(client):
    candidate = store.create_candidate(source_kind="chat", category="Work",
                                       text="Works at Acme", confidence=0.7)
    approved = client.post(f"/api/memories/candidates/{candidate['id']}/approve",
                           json={"text": "Works at Acme Corp"}).json()
    assert approved["ok"] is True
    assert approved["memory"]["text"] == "Works at Acme Corp"
    assert approved["memory"]["origin"] == "approved"
    assert client.get("/api/memories/candidates").json()["candidates"] == []


def test_rejecting_leaves_nothing_behind(client):
    candidate = store.create_candidate(source_kind="chat", category="Work",
                                       text="Hates Mondays", confidence=0.4)
    assert client.post(f"/api/memories/candidates/{candidate['id']}/reject").json() == {"ok": True}
    assert client.get("/api/memories/candidates").json()["candidates"] == []
    assert client.get("/api/memories").json()["memories"] == []


def test_a_contradicted_memory_is_reported_as_such_while_it_waits(client):
    """The other half of "a conflict always needs a person": the old, possibly
    wrong memory stops being asserted as settled fact while a candidate says the
    opposite, and the screen has to be able to say so."""
    existing = client.post("/api/memories", json={"text": "Uses a Mac"}).json()["memory"]
    store.create_candidate(source_kind="chat", category="Preferences",
                           text="Uses Windows now", confidence=0.9,
                           conflict_with=existing["id"])

    assert client.get("/api/memories").json()["conflicted"] == [existing["id"]]


def test_use_new_edits_the_contradicted_memory_rather_than_adding_a_second(client):
    """Two memories asserting opposite things is the state this exists to
    prevent, so resolving replaces rather than appends."""
    existing = client.post("/api/memories", json={"text": "Uses a Mac"}).json()["memory"]
    candidate = store.create_candidate(source_kind="chat", category="Preferences",
                                       text="Uses Windows now", confidence=0.9,
                                       conflict_with=existing["id"])

    client.post(f"/api/memories/candidates/{candidate['id']}/resolve-conflict",
                json={"choice": "use-new"})

    memories = client.get("/api/memories").json()["memories"]
    assert [m["text"] for m in memories] == ["Uses Windows now"]
    assert client.get("/api/memories").json()["conflicted"] == []


def test_keep_old_drops_the_candidate_and_changes_nothing(client):
    existing = client.post("/api/memories", json={"text": "Uses a Mac"}).json()["memory"]
    candidate = store.create_candidate(source_kind="chat", category="Preferences",
                                       text="Uses Windows now", confidence=0.9,
                                       conflict_with=existing["id"])

    client.post(f"/api/memories/candidates/{candidate['id']}/resolve-conflict",
                json={"choice": "keep-old"})
    assert [m["text"] for m in client.get("/api/memories").json()["memories"]] == ["Uses a Mac"]
    assert client.get("/api/memories/candidates").json()["candidates"] == []


def test_an_unrecognised_choice_is_refused_rather_than_guessed(client):
    candidate = store.create_candidate(source_kind="chat", category="Work",
                                       text="x", confidence=0.5)
    answer = client.post(f"/api/memories/candidates/{candidate['id']}/resolve-conflict",
                         json={"choice": "whatever"})
    assert answer.status_code == 400


# --- profile notes ---------------------------------------------------------------

def test_a_profile_note_is_a_memory_in_the_about_you_category(client):
    """Not a second store. The screens are different because the questions are
    different; the rows are the same rows."""
    client.post("/api/profile", json={"text": "Wants to ship by March"})

    entries = client.get("/api/profile").json()["entries"]
    assert [e["text"] for e in entries] == ["Wants to ship by March"]
    same = client.get("/api/memories?category=About+You").json()["memories"]
    assert [m["text"] for m in same] == ["Wants to ship by March"]


def test_notes_read_oldest_first(client):
    """The order they were written in, which is not the newest-first order the
    browse view wants."""
    for text in ("first", "second", "third"):
        client.post("/api/profile", json={"text": text})
    assert [e["text"] for e in client.get("/api/profile").json()["entries"]] \
        == ["first", "second", "third"]


def test_editing_a_note_keeps_its_history_and_deleting_removes_it(client):
    note = client.post("/api/profile", json={"text": "Learning Rust"}).json()["entry"]
    client.patch(f"/api/profile/{note['id']}", json={"text": "Learning Go"})

    versions = client.get(f"/api/profile/{note['id']}/versions").json()["versions"]
    assert [v["text"] for v in versions] == ["Learning Rust", "Learning Rust"]
    assert versions[0]["reason"] == "Edited from Profile & Goals."

    client.delete(f"/api/profile/{note['id']}")
    assert client.get("/api/profile").json()["entries"] == []


def test_an_empty_note_is_refused_on_both_write_paths(client):
    assert client.post("/api/profile", json={"text": "   "}).status_code == 400
    note = client.post("/api/profile", json={"text": "real"}).json()["entry"]
    assert client.patch(f"/api/profile/{note['id']}", json={"text": ""}).status_code == 400
