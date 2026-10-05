"""Work that outlives the call that started it, without losing it silently.

Three subsystems need the same thing: start something slow, return straight
away, and have the result reach the user through the conversation rather than
through a return value. Doing that with a bare thread each time loses two
properties that turn out to matter:

* **A test can wait for it.** `join_all()` makes "the answer arrives later" a
  testable claim rather than a sleep.
* **A failure is logged, not swallowed.** A thread that raises prints nothing
  useful by default, which is how background work fails invisibly.

Deliberately not a queue, a pool or a scheduler. Real background WORK — the kind
that needs supervision, retries and a record — is what `jarvis/jobs/` is for.
This is for a single follow-up that should not block a reply.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

logger = logging.getLogger(__name__)

_threads: list[threading.Thread] = []
_lock = threading.RLock()


def run_in_background(work: Callable[[], None], *, name: str = "background") -> threading.Thread:
    def guarded() -> None:
        try:
            work()
        except Exception:  # noqa: BLE001 — the whole point is that it is not silent
            logger.exception("background work %r failed", name)

    thread = threading.Thread(target=guarded, name=name, daemon=True)
    with _lock:
        _threads.append(thread)
    thread.start()
    return thread


def join_all(timeout: float = 20.0) -> bool:
    """Wait for everything outstanding. Returns whether they all finished.

    Called at shutdown and between tests: a daemon thread is killed abruptly at
    process exit, and one holding a database write at that moment is how a clean
    stop becomes a corrupt file.
    """
    with _lock:
        pending = [t for t in _threads if t.is_alive()]
        _threads.clear()
    for thread in pending:
        thread.join(timeout)
    return all(not t.is_alive() for t in pending)
