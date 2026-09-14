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
