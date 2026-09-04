"""The schema this port produces must be indistinguishable from the Node one.

This is the highest-stakes test in the suite. The owner's real data/jarvis.db
holds their memories, conversations, jobs and self-model history; the Python
backend is meant to open that exact file with no conversion. Every test here
therefore compares against the REAL Node migration runner rather than against a
schema this port wrote down for itself.

The DDL itself is extracted mechanically (tools/record/extract-migrations.mjs),
so what these tests really guard is the runner around it: transaction framing,
ordering, idempotency, and the three migrations carrying logic beyond DDL.
"""

from __future__ import annotations

import json
import shutil
import sqlite3

import pytest
from conftest import node_migrate, requires_node, schema_fingerprint

from jarvis import db as db_module
from jarvis.db import MIGRATION_COUNT, get_db


def _open(path):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


# --- schema equality --------------------------------------------------------


def test_fresh_database_reaches_the_current_version(scratch):
    db = get_db()
    assert db.execute("PRAGMA user_version").fetchone()[0] == MIGRATION_COUNT


@requires_node
def test_schema_matches_node_exactly(scratch, tmp_path):
    get_db()
    ours = schema_fingerprint(scratch.data_dir / "jarvis.db")

    node_dir = tmp_path / "node-data"
    node_dir.mkdir()
    node_migrate(node_dir)
    theirs = schema_fingerprint(node_dir / "jarvis.db")

    assert [o[1] for o in ours] == [t[1] for t in theirs], "object names differ"
    assert ours == theirs, "schema SQL differs"


@requires_node
def test_node_and_python_agree_on_version(scratch, tmp_path):
    get_db()
    node_dir = tmp_path / "node-data"
    node_dir.mkdir()
    node_migrate(node_dir)
    ours = _open(scratch.data_dir / "jarvis.db").execute("PRAGMA user_version").fetchone()[0]
    theirs = _open(node_dir / "jarvis.db").execute("PRAGMA user_version").fetchone()[0]
    assert ours == theirs == MIGRATION_COUNT


def test_wal_and_foreign_keys_are_on(scratch):
    db = get_db()
    assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_full_text_search_works(scratch):
    # FTS5 is the reason SQLite was chosen over a JSON file in the first place;
    # a build of Python without it would fail here rather than at runtime.
    db = get_db()
    db.execute("INSERT INTO conversations VALUES ('c1','T','t','t',0,0)")
    db.execute(
        "INSERT INTO messages (conversation_id, seq, role, text, created_at) "
        "VALUES ('c1', 1, 'user', 'the quick brown fox', 't')"
    )
    hits = db.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'brown'").fetchone()[0]
    assert hits == 1


def test_migrating_twice_changes_nothing(scratch):
    db = get_db()
    before = db.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
    db_module.migrate(db)
    db_module.migrate(db)
    assert db.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == before
    assert db.execute("PRAGMA user_version").fetchone()[0] == MIGRATION_COUNT


@requires_node
def test_opening_a_node_built_database_is_a_no_op(scratch, tmp_path):
    """The migration case that actually happens on the owner's machine."""
    node_dir = tmp_path / "node-data"
    node_dir.mkdir()
    node_migrate(node_dir)
    shutil.copy(node_dir / "jarvis.db", scratch.data_dir / "jarvis.db")
    before = schema_fingerprint(scratch.data_dir / "jarvis.db")

    db = get_db()
    assert db.execute("PRAGMA user_version").fetchone()[0] == MIGRATION_COUNT
    assert schema_fingerprint(scratch.data_dir / "jarvis.db") == before


def test_a_failing_migration_rolls_back_its_version_bump(scratch, monkeypatch):
    """A half-applied migration with its version already bumped is the one
    failure this runner must never allow — it would leave the database
    permanently wedged between two schemas."""
    conn = sqlite3.connect(str(scratch.data_dir / "boom.db"), isolation_level=None)
    conn.row_factory = sqlite3.Row

    real_apply = db_module._apply_migration

    def explode(c, version):
        if version == 3:
            real_apply(c, version)
            raise RuntimeError("simulated failure part-way through migration 3")
        return real_apply(c, version)

    monkeypatch.setattr(db_module, "_apply_migration", explode)
    with pytest.raises(RuntimeError):
        db_module.migrate(conn)

    # Migrations 1 and 2 committed; 3 rolled back cleanly and did not bump.
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert not conn.in_transaction


# --- the three migrations carrying real logic -------------------------------


LEGACY_PROFILE = {
    "entries": [
        {"id": "p1", "text": "Prefers short answers", "addedAt": "2025-01-02T03:04:05.678Z"},
        {"id": "p2", "text": "Lives in Lagos", "addedAt": "2025-02-03T04:05:06.789Z"},
        {"id": "p3", "text": ""},
    ]
}

STALE_MODELS = {
    "entries": [
        {"id": "m1", "label": "A", "availability": {"state": "unreachable", "since": 111}},
        {"id": "m2", "label": "B", "availability": {"state": "working", "since": 222}},
        {"id": "m3", "label": "C"},
        {"id": "m4", "label": "D", "availability": {"state": "unsupported"}},
    ]
}


def _seed_legacy(data_dir):
    (data_dir / "profile.json").write_text(json.dumps(LEGACY_PROFILE, indent=2), encoding="utf-8")
    (data_dir / "models.json").write_text(json.dumps(STALE_MODELS, indent=2), encoding="utf-8")


def test_migration_2_imports_the_legacy_profile(scratch):
    _seed_legacy(scratch.data_dir)
    db = get_db()
    rows = db.execute("SELECT id, category, text, source_kind, source_ref FROM memories ORDER BY id").fetchall()
    assert [r["id"] for r in rows] == ["mem_legacy_p1", "mem_legacy_p2"], "empty-text entry must be skipped"
    assert all(r["category"] == "About You" for r in rows)
    assert all(r["source_kind"] == "legacy" and r["source_ref"] == "profile.json" for r in rows)
    # Each entry becomes its own first version row, so history reads as
    # "migrated" rather than starting blank.
    versions = db.execute("SELECT memory_id, reason FROM memory_versions ORDER BY memory_id").fetchall()
    assert len(versions) == 2
    assert versions[0]["reason"] == "Migrated from the old About You notes."


def test_migration_2_seeds_the_categories(scratch):
    db = get_db()
    names = [r[0] for r in db.execute("SELECT name FROM memory_categories ORDER BY name")]
    assert names == sorted(db_module.SEED_CATEGORIES)


def test_migration_10_clears_only_stale_availability(scratch):
    _seed_legacy(scratch.data_dir)
    get_db()
    from jarvis.store import read_json

    entries = {e["id"]: e for e in read_json("models")["entries"]}
    assert "availability" not in entries["m1"], "unreachable ban must be cleared"
    assert "availability" not in entries["m4"], "unsupported state is cleared too"
    assert entries["m2"]["availability"]["state"] == "working", "a working entry is left alone"
    assert "availability" not in entries["m3"]


@requires_node
def test_legacy_migrations_match_node_byte_for_byte(scratch, tmp_path):
    _seed_legacy(scratch.data_dir)
    node_dir = tmp_path / "node-data"
    node_dir.mkdir()
    _seed_legacy(node_dir)

    get_db()
    node_migrate(node_dir)

    ours = _open(scratch.data_dir / "jarvis.db")
    theirs = _open(node_dir / "jarvis.db")
    q = "SELECT id, category, text, source_kind, source_ref, created_at, updated_at, archived FROM memories ORDER BY id"
    assert [dict(r) for r in ours.execute(q)] == [dict(r) for r in theirs.execute(q)]
    # models.json is rewritten by the migration — the file itself must match.
    assert (scratch.data_dir / "models.json").read_text(encoding="utf-8") == (
        node_dir / "models.json"
    ).read_text(encoding="utf-8")


ORPHAN_ROWS = [
    (1, "user", "hi", None),
    (2, "assistant", None, json.dumps({"toolCalls": [{"name": "get_time"}]})),   # answered -> kept
    (3, "tool", "result", None),
    (4, "assistant", None, json.dumps({"toolCalls": [{"name": "get_weather"}]})),  # orphan -> deleted
    (5, "user", "again", None),
    (6, "assistant", None, json.dumps({"toolCalls": [{"name": "open_app"}]})),   # orphan at end -> deleted
    (7, "assistant", "plain reply", json.dumps({"toolCalls": []})),              # empty -> kept
    (8, "assistant", "no payload", None),                                        # null -> kept
    (9, "assistant", "bad json", "{not json"),                                   # unparseable -> kept
]


def _seed_orphans(data_dir):
    """Build a database stopped at version 5 and plant messages for migration 6."""
    conn = sqlite3.connect(str(data_dir / "jarvis.db"), isolation_level=None)
    conn.row_factory = sqlite3.Row
    for v in range(1, 6):
        db_module._apply_migration(conn, v)
    conn.execute("PRAGMA user_version = 5")
    conn.execute("INSERT INTO conversations VALUES ('c1','T','t','t',0,0)")
    for seq, role, text, payload in ORPHAN_ROWS:
        conn.execute(
            "INSERT INTO messages (conversation_id, seq, role, text, payload, created_at) "
            "VALUES (?,?,?,?,?,?)",
            ("c1", seq, role, text, payload, "t"),
        )
    conn.close()


def test_migration_6_deletes_only_unanswered_tool_calls(scratch):
    _seed_orphans(scratch.data_dir)
    db = get_db()
    kept = [r[0] for r in db.execute("SELECT seq FROM messages ORDER BY seq")]
    assert kept == [1, 2, 3, 5, 7, 8, 9]


@requires_node
def test_migration_6_matches_node(scratch, tmp_path):
    _seed_orphans(scratch.data_dir)
    node_dir = tmp_path / "node-data"
    node_dir.mkdir()
    _seed_orphans(node_dir)

    db = get_db()
    node_migrate(node_dir)

    ours = [r[0] for r in db.execute("SELECT seq FROM messages ORDER BY seq")]
    theirs = [r[0] for r in _open(node_dir / "jarvis.db").execute("SELECT seq FROM messages ORDER BY seq")]
    assert ours == theirs
