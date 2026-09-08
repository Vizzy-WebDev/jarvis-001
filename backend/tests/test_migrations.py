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


#: Tables this build deliberately EXTENDS rather than leaves alone, and the exact
#: columns it adds. Named here so the assertions below can allow precisely this
#: and nothing else: a column appearing that is not on this list, or a Node column
#: changing, is still a failure.
EXTENDED_TABLES = {
    "memories": {"importance", "expires_at"},
    "jobs": {"priority", "progress", "current_step"},
}


def _columns(path, table):
    """{name: (type, notnull, default, pk)} — the properties an existing row
    depends on. Comparing these rather than the CREATE TABLE string is what lets
    an added column pass while a changed one does not."""
    conn = _open(path)
    try:
        return {r["name"]: (r["type"], r["notnull"], r["dflt_value"], r["pk"])
                for r in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _open(path):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


# --- schema equality --------------------------------------------------------


def test_fresh_database_reaches_the_current_version(scratch):
    db = get_db()
    assert db.execute("PRAGMA user_version").fetchone()[0] == MIGRATION_COUNT


@requires_node
def test_python_schema_is_a_superset_of_nodes(scratch, tmp_path):
    """Python now adds tables of its own (approvals, permission_grants), so the
    schemas are no longer identical — but every object Node creates must still
    exist in Python, character for character. That is what lets the port open the
    owner's real database, and it is the part that must never drift."""
    get_db()
    ours = {(t, n): sql for t, n, sql in schema_fingerprint(scratch.data_dir / "jarvis.db")}

    node_dir = tmp_path / "node-data"
    node_dir.mkdir()
    node_migrate(node_dir)
    theirs = {(t, n): sql for t, n, sql in schema_fingerprint(node_dir / "jarvis.db")}

    missing = sorted(k for k in theirs if k not in ours)
    assert not missing, f"Python is missing objects Node creates: {missing}"

    differing = sorted(k for k in theirs if ours[k] != theirs[k])
    # A table this build extends differs in SQL by design; the property that
    # matters there is that every column Node defines survives UNCHANGED, and
    # that the only additions are the ones we meant to make.
    extended = {("table", name) for name in EXTENDED_TABLES}
    assert not [k for k in differing if k not in extended], (
        f"shared objects differ in SQL: {[k for k in differing if k not in extended]}")

    for name, added in EXTENDED_TABLES.items():
        mine = _columns(scratch.data_dir / "jarvis.db", name)
        node = _columns(node_dir / "jarvis.db", name)
        for column, definition in node.items():
            assert column in mine, f"{name}.{column} disappeared"
            assert mine[column] == definition, f"{name}.{column} was altered"
        assert set(mine) - set(node) == added, f"{name} gained unexpected columns"


@requires_node
def test_the_only_extra_objects_are_this_builds_own(scratch, tmp_path):
    """A superset assertion alone would hide an accidental table. Name what we
    add, so anything else appearing is a failure rather than a shrug."""
    get_db()
    ours = {n for _t, n, _sql in schema_fingerprint(scratch.data_dir / "jarvis.db")}

    node_dir = tmp_path / "node-data"
    node_dir.mkdir()
    node_migrate(node_dir)
    theirs = {n for _t, n, _sql in schema_fingerprint(node_dir / "jarvis.db")}

    expected_extra = {
        "approvals", "idx_approvals_status", "idx_approvals_session",
        "idx_approvals_operation",
        "permission_grants", "idx_grants_capability",
        "operations", "idx_operations_session",
        # SQLite creates these itself for a TEXT PRIMARY KEY. Listed rather than
        # filtered out, so the assertion stays exact.
        "sqlite_autoindex_approvals_1", "sqlite_autoindex_permission_grants_1",
        "sqlite_autoindex_operations_1",
        # Memory's expiry index (§22): an expired memory has to be filterable
        # without scanning the table on every prompt build.
        "idx_memories_expires",
        # Jobs by priority (§13): "what should I look at first" is a query, not
        # a sort over everything.
        "idx_jobs_priority",
    }
    assert (ours - theirs) == expected_extra


@requires_node
def test_node_and_python_agree_on_version(scratch, tmp_path):
    get_db()
    node_dir = tmp_path / "node-data"
    node_dir.mkdir()
    node_migrate(node_dir)
    ours = _open(scratch.data_dir / "jarvis.db").execute("PRAGMA user_version").fetchone()[0]
    theirs = _open(node_dir / "jarvis.db").execute("PRAGMA user_version").fetchone()[0]
    assert ours == MIGRATION_COUNT
    # Python is ahead by its own migrations. Node's loop runs `v < MIGRATIONS.length`
    # with 19 entries, so a database already past that does no work there — which
    # is what makes adding steps here safe while the Node app still runs.
    assert ours > theirs, "Python should be ahead of Node's migration count"


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
def test_opening_a_node_built_database_adds_only_new_objects(scratch, tmp_path):
    """The migration case that actually happens on the owner's machine.

    No longer a no-op — Python's own migrations run — so the property that
    matters is stronger and more specific: everything that already existed must
    survive untouched, byte for byte. Nothing Node created may be altered,
    renamed or dropped on the way past.
    """
    node_dir = tmp_path / "node-data"
    node_dir.mkdir()
    node_migrate(node_dir)
    shutil.copy(node_dir / "jarvis.db", scratch.data_dir / "jarvis.db")
    before = {(t, n): sql for t, n, sql in schema_fingerprint(scratch.data_dir / "jarvis.db")}

    db = get_db()
    assert db.execute("PRAGMA user_version").fetchone()[0] == MIGRATION_COUNT

    after = {(t, n): sql for t, n, sql in schema_fingerprint(scratch.data_dir / "jarvis.db")}
    for key, sql in before.items():
        assert key in after, f"{key} disappeared when Python opened the database"
        if key in {("table", name) for name in EXTENDED_TABLES}:
            continue        # checked column-by-column below, which is stricter
        assert after[key] == sql, f"{key} was altered when Python opened the database"

    for name, added in EXTENDED_TABLES.items():
        node = _columns(node_dir / "jarvis.db", name)
        mine = _columns(scratch.data_dir / "jarvis.db", name)
        assert all(mine.get(c) == d for c, d in node.items()), (
            f"an existing {name} column changed when Python opened the database")
        assert set(mine) - set(node) == added


@requires_node
def test_existing_rows_survive_the_new_migrations(scratch, tmp_path):
    """Schema surviving is not the same as data surviving. §40: do not destroy
    existing data."""
    node_dir = tmp_path / "node-data"
    node_dir.mkdir()
    node_migrate(node_dir)

    seeded = sqlite3.connect(str(node_dir / "jarvis.db"), isolation_level=None)
    seeded.execute("INSERT INTO conversations VALUES ('c1','Real chat','t','t',0,0)")
    seeded.execute(
        "INSERT INTO messages (conversation_id, seq, role, text, created_at) "
        "VALUES ('c1', 1, 'user', 'do not lose me', 't')"
    )
    seeded.close()
    shutil.copy(node_dir / "jarvis.db", scratch.data_dir / "jarvis.db")

    db = get_db()
    assert db.execute("SELECT title FROM conversations WHERE id='c1'").fetchone()[0] == "Real chat"
    assert db.execute("SELECT text FROM messages WHERE conversation_id='c1'").fetchone()[0] == "do not lose me"


@requires_node
def test_a_memory_written_by_node_survives_the_new_columns(scratch, tmp_path):
    """Migration 22 alters the table the owner's real memories live in. An
    ALTER that loses a row is the worst outcome in this whole port."""
    node_dir = tmp_path / "node-data"
    node_dir.mkdir()
    node_migrate(node_dir)

    seeded = sqlite3.connect(str(node_dir / "jarvis.db"), isolation_level=None)
    seeded.execute(
        "INSERT INTO memories (id, category, text, source_kind, created_at, updated_at, "
        "archived, origin) VALUES ('mem_1','About You','Drinks tea','chat','t','t',0,'approved')")
    seeded.close()
    shutil.copy(node_dir / "jarvis.db", scratch.data_dir / "jarvis.db")

    db = get_db()
    row = db.execute("SELECT * FROM memories WHERE id='mem_1'").fetchone()
    assert row["text"] == "Drinks tea" and row["origin"] == "approved"
    # The new columns exist and are NULL, not a value nobody chose.
    assert row["importance"] is None and row["expires_at"] is None


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
