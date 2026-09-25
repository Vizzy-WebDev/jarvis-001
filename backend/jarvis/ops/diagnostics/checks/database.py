"""Can the database actually be written to and read back?

A round trip, not a connection test: an open handle to a file on a full disk, or
one whose journal cannot be written, connects perfectly and fails on the first
real write. The row is written under a fixed key and removed again, so the check
leaves nothing behind.
"""

from __future__ import annotations

from typing import Any

from ..registry import Check

KEY = "ops.diagnostics.canary"


def probe() -> dict[str, Any]:
    from ....db import get_db

    try:
        db = get_db()
        db.execute("INSERT INTO app_state (key, value) VALUES (?, ?) "
                   "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (KEY, "1"))
        row = db.execute("SELECT value FROM app_state WHERE key = ?", (KEY,)).fetchone()
        db.execute("DELETE FROM app_state WHERE key = ?", (KEY,))
    except Exception as err:  # noqa: BLE001
        return {"ok": False, "detail": f"The database could not be written to: {err}"}
    if row is None or row["value"] != "1":
        return {"ok": False, "detail": "A row written to the database did not read back."}
    return {"ok": True}


CHECK = Check(id="database", probe=probe)
