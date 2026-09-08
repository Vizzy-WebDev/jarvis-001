"""Recorded system-load samples. A leaf: the database and the clock, nothing else."""

from __future__ import annotations

from typing import Any

from ...db import get_db
from ...jscompat import now_iso

#: Roughly a week at the sampler's own cadence. Long enough for a real baseline,
#: short enough that the table never becomes something to worry about.
KEEP_ROWS = 20_000


def record_sample(*, cpu_pct: float | None, mem_free_pct: float | None,
                  rss_bytes: int | None) -> None:
    get_db().execute(
        "INSERT INTO env_samples (ts, cpu_pct, mem_free_pct, rss_bytes) VALUES (?, ?, ?, ?)",
        (now_iso(), cpu_pct, mem_free_pct, rss_bytes))


def list_recent(*, since_iso: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
    if since_iso:
        rows = get_db().execute(
            "SELECT * FROM env_samples WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
            (since_iso, limit)).fetchall()
    else:
        rows = get_db().execute(
            "SELECT * FROM env_samples ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    return [{"ts": r["ts"], "cpuPct": r["cpu_pct"], "memFreePct": r["mem_free_pct"],
             "rssBytes": r["rss_bytes"]} for r in rows]


def prune_old(keep: int = KEEP_ROWS) -> int:
    cursor = get_db().execute(
        "DELETE FROM env_samples WHERE id NOT IN "
        "(SELECT id FROM env_samples ORDER BY id DESC LIMIT ?)", (keep,))
    return int(cursor.rowcount or 0)
