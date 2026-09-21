"""The single SQLite connection for Chat History, Memory, Jobs, Self-Improvement,
Self-Model and Operational Awareness — the only module in the backend that
knows SQLite exists. Everything else goes through chat_store.py or a subsystem's
own store module, never through this file's connection handle directly.

The schema is stable: the DDL in migrations.py is never rewritten, so the owner's
existing data/jarvis.db opens as a no-op.

Two details worth knowing:

1. `sqlite3.Connection.executescript()` is NOT used, even though it would take
   whole multi-statement scripts. executescript() issues
   an implicit COMMIT before it runs, which would silently break the explicit
   BEGIN/COMMIT wrapper each migration runs inside — a half-applied migration
   with its user_version already bumped is exactly the failure this file must
   never allow. Statements are split and executed individually instead.

2. The split uses `sqlite3.complete_statement()` rather than splitting on ';'.
   Three of the migrations create FTS5 sync triggers whose BEGIN...END bodies
   contain their own semicolons; a naive split would cut them in half.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from .migrations import MIGRATION_SQL
from .migrations_extra import EXTRA_MIGRATION_SQL
from .store import data_dir, read_json, write_json

_db: sqlite3.Connection | None = None
_lock = threading.RLock()

SEED_CATEGORIES = [
    "Preferences",
    "Projects",
    "Work",
    "Learning",
    "People",
    "Long-term Goals",
    "About You",
    "Uncategorized",
]


def _split_sql(script: str) -> list[str]:
    """Split a multi-statement SQL script into individually executable statements.

    Uses sqlite3.complete_statement() so a CREATE TRIGGER's BEGIN...END body —
    which contains its own semicolons — is kept whole.
    """
    statements: list[str] = []
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            stripped = buf.strip()
            if stripped:
                statements.append(stripped)
            buf = ""
    tail = buf.strip()
    if tail:
        statements.append(tail)
    return statements


def _now_iso() -> str:
    """An ISO-8601 timestamp with milliseconds and a trailing 'Z' — the shape every
    stored timestamp uses, since a differently-shaped one would sort wrongly against
    the rows already on disk."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"
    )


# --- The three migrations that carry real logic beyond DDL -------------------


def _migration_2_extra(conn: sqlite3.Connection) -> None:
    """Seed the memory categories, then perform the one-time import of the old
    data/profile.json "About You" store into Memory.

    Tied to this migration step rather than a startup check specifically so it
    runs exactly once, ever — the same guarantee PRAGMA user_version already
    gives every other step, with no separate "have we migrated yet?" flag to
    maintain. Each entry becomes both a memory AND its own first version row, so
    its history reads as "migrated" rather than starting blank.
    """
    for name in SEED_CATEGORIES:
        conn.execute(
            "INSERT OR IGNORE INTO memory_categories (name, status) VALUES (?, ?)",
            (name, "approved"),
        )

    legacy = read_json("profile", {"entries": []}) or {}
    entries = legacy.get("entries") or []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("text"):
            continue
        suffix = entry.get("id") or uuid.uuid4().hex[:11]
        mem_id = f"mem_legacy_{suffix}"
        ts = entry.get("addedAt") or _now_iso()
        conn.execute(
            "INSERT INTO memories (id, category, text, source_kind, source_ref, "
            "created_at, updated_at, archived) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            (mem_id, "About You", entry["text"], "legacy", "profile.json", ts, ts),
        )
        conn.execute(
            "INSERT INTO memory_versions (memory_id, text, category, changed_at, reason) "
            "VALUES (?, ?, ?, ?, ?)",
            (mem_id, entry["text"], "About You", ts,
             "Migrated from the old About You notes."),
        )


def _migration_6(conn: sqlite3.Connection) -> None:
    """One-off repair for a real, now-fixed bug: a turn that failed between
    writing a tool-call step and writing its result used to leave the tool-call
    message behind forever.

    A lone tool-call with no result is exactly what every adapter replays as an
    unanswered function/tool call on every later turn — which every provider's
    API rejects outright, so the conversation's whole model fallback chain fails
    turn after turn.
    """
    rows = conn.execute(
        "SELECT id, conversation_id, seq, payload FROM messages "
        "WHERE role = 'assistant' AND payload IS NOT NULL"
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (ValueError, TypeError):
            continue
        tool_calls = payload.get("toolCalls") if isinstance(payload, dict) else None
        if not isinstance(tool_calls, list) or not tool_calls:
            continue
        nxt = conn.execute(
            "SELECT role FROM messages WHERE conversation_id = ? AND seq > ? "
            "ORDER BY seq ASC LIMIT 1",
            (row["conversation_id"], row["seq"]),
        ).fetchone()
        if nxt is None or nxt["role"] != "tool":
            conn.execute("DELETE FROM messages WHERE id = ?", (row["id"],))


def _migration_10(conn: sqlite3.Connection) -> None:
    """One-time reset of stale model-availability bans written under an older,
    over-eager rule, so they don't keep outliving the fix that stops new ones.

    The one migration that touches data/models.json rather than this SQLite file,
    since availability lives in that JSON store. Goes through store.py's
    read_json/write_json, which honour JARVIS_DATA_DIR — never a hardcoded path.
    """
    data = read_json("models", None)
    if not isinstance(data, dict) or not data.get("entries"):
        return
    changed = False
    for entry in data["entries"]:
        availability = entry.get("availability")
        if availability and availability.get("state") != "working":
            del entry["availability"]
            changed = True
    if changed:
        write_json("models", data)


_EXTRA_STEPS: dict[int, Callable[[sqlite3.Connection], None]] = {
    2: _migration_2_extra,
    6: _migration_6,
    10: _migration_10,
}

# The original schema steps plus the later ones. Kept as one ordered mapping so
# migrate() stays a single loop over consecutive versions.
ALL_MIGRATION_SQL: dict[int, list[str]] = {**MIGRATION_SQL, **EXTRA_MIGRATION_SQL}
MIGRATION_COUNT = max(ALL_MIGRATION_SQL)


def _apply_migration(conn: sqlite3.Connection, version: int) -> None:
    """Run one migration's DDL then its extra logic, if it has any."""
    for script in ALL_MIGRATION_SQL.get(version, []):
        for statement in _split_sql(script):
            conn.execute(statement)
    extra = _EXTRA_STEPS.get(version)
    if extra:
        extra(conn)


def migrate(conn: sqlite3.Connection) -> None:
    """Bring `conn` up to the current schema, one migration per transaction.

    Each step runs inside its own BEGIN/COMMIT and
    bumps user_version within that same transaction, so a failure mid-step rolls
    back both the schema change and the version bump together.
    """
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version in range(current + 1, MIGRATION_COUNT + 1):
        conn.execute("BEGIN")
        try:
            _apply_migration(conn, version)
            # PRAGMA does not accept bound parameters; `version` is a loop
            # counter over our own migration table, never caller input.
            conn.execute(f"PRAGMA user_version = {version}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def get_db() -> sqlite3.Connection:
    """The shared connection, opened (and migrated) on first use."""
    global _db
    with _lock:
        if _db is not None:
            return _db
        file = data_dir() / "jarvis.db"
        # isolation_level=None puts the driver in autocommit mode so the explicit
        # BEGIN/COMMIT above is the only transaction control in play — Python's
        # own implicit transaction handling would otherwise fight it.
        #
        # `cached_statements=0` because this one connection is shared by every thread
        # the server runs requests on. The driver keeps prepared statements per
        # connection, keyed by their SQL, so two threads running the same query at
        # once trip over one statement object — "bad parameter or other API misuse",
        # or the wrong rows handed to the wrong caller. Measured: the same threaded
        # load failed 3 runs in 3 with the cache and passed 3 in 3 without it. What
        # it costs is preparing each statement every time, which is microseconds.
        conn = sqlite3.connect(str(file), isolation_level=None, check_same_thread=False,
                               cached_statements=0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        migrate(conn)
        _db = conn
        return _db


def reset_for_tests() -> None:
    """Test-only escape hatch: forgets the cached connection so a fresh
    JARVIS_DATA_DIR takes effect on the next get_db() call. Production code
    never calls this — the connection lives for the process lifetime.

    **It deliberately does NOT close the connection, and that is the whole
    point of this function's current shape.** It used to, and the result was an
    intermittent interpreter SEGFAULT — not an exception, a hard crash that
    took the whole test run down with it:

        Fatal Python error: Segmentation fault
          chat_store.py in get_messages_since
          memory/review.py in checkpoint_conversation
          session.py in _run                      <- a daemon thread, mid-query
          db.py in reset_for_tests                <- the main thread, closing

    `_lock` guards handing the connection OUT; it does not guard using it.
    `get_db()` returns the handle and releases the lock, and every caller then
    executes with no lock held — so a background thread (Memory's own
    checkpoint runs on one, by design) can be inside `execute()` at the exact
    moment a test's teardown closes the handle underneath it. Closing a
    sqlite3 connection while another thread is executing on it is undefined
    behaviour, and this is what it looks like.

    Dropping the reference instead gets the guarantee for free: CPython closes
    the connection in its deallocator once the last reference is gone, and a
    thread that called `get_db()` holds one for as long as it is using it. So
    the close happens exactly when it is safe — after every thread that got the
    handle has finished with it — rather than whenever teardown happens to run.

    The alternative, a lock held across every query, would serialise all
    database access in a process that deliberately does real work on background
    threads. That is a large cost to fix a hazard only the test hook creates.
    """
    global _db
    _db = None
