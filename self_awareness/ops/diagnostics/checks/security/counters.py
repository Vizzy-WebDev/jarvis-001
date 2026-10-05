"""The tally the security checks compare against.

A leaf over `ops_security_events`: one row per noteworthy occurrence, counted
over a window. Kept apart from the trace on purpose — the trace answers "what
was this subsystem doing", and this answers "how often has this kind of thing
happened lately", which is a different question with a different shape.
"""

from __future__ import annotations

from .....db import get_db
from .....jscompat import now_iso


def record(kind: str) -> None:
    get_db().execute("INSERT INTO ops_security_events (kind, ts) VALUES (?, ?)",
                     (kind, now_iso()))


def count_since(kind: str, since_iso: str) -> int:
    row = get_db().execute(
        "SELECT COUNT(*) AS n FROM ops_security_events WHERE kind = ? AND ts >= ?",
        (kind, since_iso)).fetchone()
    return int(row["n"] or 0)


def prune_before(before_iso: str) -> int:
    cursor = get_db().execute("DELETE FROM ops_security_events WHERE ts < ?", (before_iso,))
    return int(cursor.rowcount or 0)
