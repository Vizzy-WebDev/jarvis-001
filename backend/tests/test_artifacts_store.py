"""The artifact store: which chat a file belongs to, paging, kinds, and delete.

Each claim here is one the Artifacts page and "Open in Chat" depend on:

* a file records the conversation that made it — including a specialist's work
  in that conversation — and records NO chat for work nobody is chatting in;
* every artifact is reachable, not just the newest twenty;
* delete really removes the file and the record;
* making a file leaves nothing behind in the temp folder.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from jarvis import artifacts, chat_store
from jarvis.db import get_db
from jarvis.db import reset_for_tests as reset_db


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    yield
    reset_db()


def _made(name: str, content: str = "x", **kwargs) -> artifacts.Artifact:
    staged = artifacts.staging_path(name)
    staged.write_text(content)
    return artifacts.keep(staged, name=name, **kwargs)


# --- which conversation ------------------------------------------------------------------------

def test_a_file_made_in_a_conversation_records_that_conversation():
    chat = chat_store.create_conversation()["id"]
    kept = _made("notes.md", session_id=chat)
    stored = artifacts.get(kept.id)
    assert stored.session_id == chat and stored.conversation_id == chat


def test_a_specialists_file_belongs_to_the_conversation_it_was_asked_in():
    chat = chat_store.create_conversation()["id"]
    kept = _made("plan.md", session_id=f"agent:writer:{chat}")
    assert artifacts.get(kept.id).conversation_id == chat


@pytest.mark.parametrize("session", [None, "job:abc123", "agent:writer:solo", "no-such-chat"])
def test_work_nobody_is_chatting_in_has_no_conversation(session):
    assert artifacts.get(_made("r.txt", session_id=session).id).conversation_id is None


def test_a_title_is_kept_and_the_filename_is_the_fallback():
    titled = _made("q3.md", title="Q3 summary\nfor the board")
    assert titled.as_result()["title"] == "Q3 summary for the board"
    assert _made("plain.md").as_result()["title"] == "plain.md"


# --- kinds ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("name,kind", [
    ("a.docx", "document"), ("a.xlsx", "spreadsheet"), ("a.pptx", "presentation"),
    ("a.pdf", "pdf"), ("a.md", "markdown"), ("a.html", "web"), ("a.svg", "image"),
    ("a.png", "image"), ("a.mp3", "audio"), ("a.csv", "data"), ("a.json", "data"),
    ("a.py", "code"), ("a.TS", "code"), ("a.txt", "text"), ("a.bin", "other"), ("noext", "other"),
])
def test_every_file_has_one_kind(name, kind):
    assert artifacts.kind_for(name) == kind


def test_source_code_is_served_as_plain_text_never_as_something_runnable():
    assert artifacts.store.mime_for("script.js") == "text/plain"
    assert artifacts.store.mime_for("thing.bin") == "application/octet-stream"


# --- listing -------------------------------------------------------------------------------------

def test_every_artifact_is_reachable_a_page_at_a_time_newest_first():
    made = [_made(f"n{i:02}.txt").id for i in range(25)]
    seen: list[str] = []
    cursor = None
    while True:
        page, cursor = artifacts.list_page(limit=10, before=cursor)
        seen += [a.id for a in page]
        if cursor is None:
            break
    assert len(seen) == 25 and set(seen) == set(made)
    stamps = [artifacts.get(i).created_at for i in seen]
    assert stamps == sorted(stamps, reverse=True)


def test_artifacts_made_in_the_same_millisecond_still_page_without_loss():
    for i in range(7):
        _made(f"s{i}.txt")
    get_db().execute("UPDATE artifacts SET created_at = '2026-01-01T00:00:00.000Z'")
    seen, cursor = [], None
    while True:
        page, cursor = artifacts.list_page(limit=3, before=cursor)
        seen += [a.id for a in page]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 7


def test_search_matches_name_or_title_and_filter_matches_kind():
    _made("budget.xlsx.csv", title="Household budget")
    _made("notes.md")
    _made("script.py")
    _made("mystery.bin")
    assert [a.name for a in artifacts.list_page(q="household")[0]] == ["budget.xlsx.csv"]
    assert [a.name for a in artifacts.list_page(q="notes")[0]] == ["notes.md"]
    assert [a.name for a in artifacts.list_page(kind="code")[0]] == ["script.py"]
    assert [a.name for a in artifacts.list_page(kind="other")[0]] == ["mystery.bin"]
    assert artifacts.list_page(q="100%_")[0] == []  # LIKE wildcards are literal


# --- delete --------------------------------------------------------------------------------------

def test_delete_removes_the_file_and_the_record():
    kept = _made("gone.md", "# bye")
    assert kept.path.exists()
    assert artifacts.delete(kept.id) is True
    assert not kept.path.exists()
    assert artifacts.get(kept.id) is None
    assert artifacts.delete(kept.id) is False


def test_deleting_a_conversation_leaves_its_files_listed_with_no_live_chat():
    chat = chat_store.create_conversation()["id"]
    kept = _made("keep.md", session_id=chat)
    chat_store.purge_conversation(chat)
    assert artifacts.get(kept.id) is not None


# --- nothing left behind -------------------------------------------------------------------------

def test_making_a_file_leaves_no_staging_folder_behind():
    staged = artifacts.staging_path("x.md")
    staged.write_text("hi")
    artifacts.keep(staged)
    assert not staged.parent.exists()


def test_a_broken_file_leaves_no_staging_folder_behind_either():
    staged = artifacts.staging_path("broken.docx")
    staged.write_bytes(b"not a zip")
    with pytest.raises(ValueError):
        artifacts.keep(staged)
    assert not staged.parent.exists()


# --- migration 34 --------------------------------------------------------------------------------

def test_migration_34_links_existing_rows_to_real_conversations_only(tmp_path):
    from jarvis import db as db_module

    conn = sqlite3.connect(str(tmp_path / "old.db"), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    for version in range(1, 34):
        conn.execute("BEGIN")
        db_module._apply_migration(conn, version)
        conn.execute(f"PRAGMA user_version = {version}")
        conn.execute("COMMIT")
    conn.execute("INSERT INTO conversations (id, title, created_at, updated_at) "
                 "VALUES ('c1', 't', 'x', 'x')")
    for art, session in (("art_a", "c1"), ("art_b", "job:1"), ("art_c", None)):
        conn.execute("INSERT INTO artifacts (id, name, mime_type, size, session_id, created_at) "
                     "VALUES (?, 'f.md', 'text/markdown', 1, ?, 'x')", (art, session))

    db_module.migrate(conn)

    linked = {r["id"]: r["conversation_id"] for r in conn.execute("SELECT * FROM artifacts")}
    assert linked == {"art_a": "c1", "art_b": None, "art_c": None}
    assert "title" in {r[1] for r in conn.execute("PRAGMA table_info(artifacts)")}


def test_the_artifacts_folder_is_under_the_scratch_data_dir(scratch):
    assert Path(artifacts.artifacts_dir()).is_relative_to(scratch.data_dir)
