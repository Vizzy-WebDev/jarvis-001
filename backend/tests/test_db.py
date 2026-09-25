"""The one SQLite connection, and the hazard in sharing it across threads.

Everything else in this project reaches the database through `chat_store.py` or
a subsystem's own store, never through the handle directly — so this file is
small on purpose. It exists for the one thing those stores cannot assert about
themselves: what happens to a connection several threads are holding at once.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

import jarvis.db as db_module


@pytest.fixture(autouse=True)
def _isolated(scratch):
    db_module.reset_for_tests()
    yield
    db_module.reset_for_tests()


def test_resetting_does_not_close_a_connection_another_thread_is_using():
    """The mechanism behind an intermittent SEGFAULT — a hard interpreter crash
    that took whole test runs down, not an exception anything could catch:

        Fatal Python error: Segmentation fault
          chat_store.py in get_messages_since
          memory/review.py in checkpoint_conversation
          session.py in _run                      <- a daemon thread, mid-query
          db.py in reset_for_tests                <- the main thread, closing

    `db._lock` guards handing the connection OUT, not using it: `get_db()`
    returns the handle and releases the lock, and callers then execute with no
    lock held. Memory's checkpoint deliberately runs on a background thread, so
    it could be inside `execute()` at the moment a test's teardown closed the
    handle underneath it — undefined behaviour in sqlite3.

    Asserted through the mechanism rather than by stress-looping, because a
    stress test for a segfault cannot report its own failure: it kills the
    runner. The old behaviour raises `ProgrammingError: Cannot operate on a
    closed database` below, deterministically.
    """
    held = db_module.get_db()
    assert held.execute("SELECT 1 AS n").fetchone()["n"] == 1

    db_module.reset_for_tests()

    try:
        assert held.execute("SELECT 1 AS n").fetchone()["n"] == 1
    except sqlite3.ProgrammingError as err:  # pragma: no cover - the old bug
        raise AssertionError(f"reset closed a connection still in use: {err}") from err


def test_resetting_still_hands_out_a_fresh_connection_afterwards():
    """Guards the fix: a reset that simply did nothing would also pass the test
    above, and would break the only reason the hook exists — a new
    JARVIS_DATA_DIR taking effect on the next call."""
    first = db_module.get_db()
    db_module.reset_for_tests()
    second = db_module.get_db()

    assert second is not first
    assert second.execute("SELECT 1 AS n").fetchone()["n"] == 1


def test_a_reset_racing_a_working_thread_never_takes_the_process_down():
    """The real shape of it, run for real. Under the old code this is the loop
    that crashed the interpreter rather than failing."""
    stop = threading.Event()
    failures: list[BaseException] = []

    def work() -> None:
        while not stop.is_set():
            try:
                db_module.get_db().execute("SELECT 1").fetchone()
            except sqlite3.ProgrammingError as err:
                failures.append(err)
                return

    threads = [threading.Thread(target=work, daemon=True) for _ in range(4)]
    for thread in threads:
        thread.start()
    try:
        for _ in range(200):
            db_module.reset_for_tests()
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=5)

    assert failures == []


def test_the_same_query_from_many_threads_at_once_does_not_trip_over_itself():
    """The other half of the hazard: not the connection being closed under a thread,
    but readers and writers running the SAME SQL on it at the same moment.

    The driver keeps prepared statements per connection, keyed by their text, so
    two threads reach for one statement object. The result is not only an error
    ("another row available", "bad parameter or other API misuse") but rows handed
    to the WRONG caller — which is silent. The connection is opened without that
    cache for exactly this reason. Measured on this workload: hundreds of errors and
    dozens of wrong rows per run with the cache on, none with it off."""
    connection = db_module.get_db()
    connection.execute("CREATE TABLE IF NOT EXISTS probe_things (id TEXT PRIMARY KEY, v TEXT)")
    connection.execute("CREATE TABLE IF NOT EXISTS probe_links (pid TEXT, mid TEXT, PRIMARY KEY (pid, mid))")
    failures: list[str] = []
    wrong: list[tuple] = []

    def reader() -> None:
        for _ in range(60):
            try:
                for row in connection.execute("SELECT * FROM probe_things ORDER BY id").fetchall():
                    connection.execute("SELECT * FROM probe_links WHERE pid = ?", (row["id"],)).fetchall()
                    again = connection.execute("SELECT * FROM probe_things WHERE id = ?", (row["id"],)).fetchone()
                    if again is not None and again["id"] != row["id"]:
                        wrong.append((row["id"], again["id"]))
            except BaseException as err:  # noqa: BLE001 — any failure at all is the point
                failures.append(f"{type(err).__name__}: {err}")

    def writer(n: int) -> None:
        for i in range(60):
            try:
                key = f"p{n}-{i % 5}"
                connection.execute("INSERT INTO probe_things (id, v) VALUES (?, ?) "
                                   "ON CONFLICT(id) DO UPDATE SET v = excluded.v", (key, str(i)))
                connection.execute("INSERT OR REPLACE INTO probe_links (pid, mid) VALUES (?, ?)", (key, str(i)))
                connection.execute("DELETE FROM probe_links WHERE pid = ? AND mid = ?", (key, str(i - 1)))
                if i % 3 == 0:
                    connection.execute("DELETE FROM probe_things WHERE id = ?", (key,))
            except BaseException as err:  # noqa: BLE001
                failures.append(f"{type(err).__name__}: {err}")

    threads = [threading.Thread(target=reader) for _ in range(5)]
    threads += [threading.Thread(target=writer, args=(n,)) for n in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(120)

    assert failures == [] and wrong == []
