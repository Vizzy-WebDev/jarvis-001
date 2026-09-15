"""Did the secrets file change without this build having written it?

**Compared by the CONTENT HASH of what this app itself last wrote, not by how
recently it wrote something.** The time-window version of this check has a real
hole: any external change landing inside the same window is explained away by a
write it had nothing to do with. A hash cannot be fooled that way — either the
file is byte-for-byte what was last written, or something else changed it.

Never reports the file's contents, or any part of one. A check on a secrets file
that logs what it found would be the leak it exists to detect.
"""

from __future__ import annotations

import hashlib
from typing import Any

from .....db import get_db
from .....jscompat import now_iso
from ...registry import Check

STATE_KEY = "ops.config_integrity.last_seen"


def _hash_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _remembered() -> str | None:
    row = get_db().execute("SELECT value FROM app_state WHERE key = ?", (STATE_KEY,)).fetchone()
    return row["value"] if row is not None else None


def _remember(digest: str) -> None:
    get_db().execute("INSERT INTO app_state (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (STATE_KEY, digest))


def probe() -> dict[str, Any]:
    from .....config import env_file_path, env_last_write_time

    path = env_file_path()
    if not path.exists():
        return {"ok": True}              # nothing configured yet is not a fault

    try:
        digest = _hash_of(path.read_text(encoding="utf-8"))
    except Exception as err:  # noqa: BLE001
        return {"ok": False, "detail": f"The settings file could not be read: {err}"}

    own = (env_last_write_time() or {}).get("hash")
    if own == digest:
        _remember(digest)
        return {"ok": True}              # exactly what this build last wrote

    known = _remembered()
    if known is None:
        # First sight of the file this install — nothing to compare against, so
        # record it rather than reporting a change nobody can confirm happened.
        _remember(digest)
        return {"ok": True}
    if known == digest:
        return {"ok": True}              # unchanged since it was last seen

    _remember(digest)
    from . import counters
    counters.record("config_changed")
    return {"ok": False,
            "detail": ("The settings file holding API keys changed, and it was not this "
                       "app that changed it. Worth confirming that was you — "
                       f"noticed at {now_iso()}.")}


CHECK = Check(id="config_integrity", probe=probe, kind="security")
