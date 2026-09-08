"""Where files attached in conversation land.

Kept rather than deleted after reading, so asking a second question about a video
does not mean uploading it again — and pruned by age and count so the folder
cannot grow without limit.

**The id IS the stored filename.** It is sanitised on the way in and carries a
timestamp prefix, which means no in-memory index to keep in sync and an id that
still resolves after a restart — which matters, because the browser holds ids
between attaching a file and sending the message.

Every id that arrives from outside is re-validated here. The browser supplies
both the original filename and, later, the id, and neither is trusted: a name
like `..\\..\\.env` would otherwise let an upload escape the folder entirely.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any

from .store import data_dir

logger = logging.getLogger(__name__)

MAX_FILES = 30
MAX_AGE_S = 7 * 24 * 60 * 60
#: A conservative set: everything else becomes an underscore.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def upload_dir() -> Path:
    """Under the same overridable data directory as everything else, so a test
    run never writes into the user's real folder."""
    target = data_dir() / "uploads"
    target.mkdir(parents=True, exist_ok=True)
    return target


def safe_name(name: str | None) -> str:
    base = Path(str(name or "upload")).name
    cleaned = _UNSAFE.sub("_", base).lstrip(".")[-80:] or "upload"
    return f"{int(time.time() * 1000):x}-{cleaned}"


def prune() -> int:
    """Drop the oldest once there are too many, and anything simply old.
    Failures are ignored: tidying up must never break an upload."""
    try:
        entries = sorted(((p, p.stat().st_mtime) for p in upload_dir().iterdir() if p.is_file()),
                         key=lambda pair: pair[1], reverse=True)
    except OSError:
        return 0

    now = time.time()
    removed = 0
    for index, (path, mtime) in enumerate(entries):
        if index < MAX_FILES and now - mtime < MAX_AGE_S:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass                         # already gone, or in use
    return removed


def save_upload(data: bytes, original_name: str | None) -> dict[str, Any]:
    """Write an uploaded body and describe it. Raises on an empty body, so an
    empty POST fails loudly rather than leaving a zero-byte file to confuse
    whatever reads it later."""
    if not data:
        raise ValueError("That upload was empty.")
    identifier = safe_name(original_name)
    target = upload_dir() / identifier
    target.write_bytes(data)
    prune()
    return {"id": identifier, "path": str(target),
            "name": Path(str(original_name or "upload")).name, "size": len(data)}


def get_upload(upload_id: str | None) -> dict[str, Any] | None:
    """Resolve an id back to its file, or None.

    The id arrives from the browser, so the character set is checked again here:
    an id such as `../../.env` must not become a path outside this folder no
    matter how it got here. The resolved path is then confirmed to be inside the
    directory as well — belt and braces, the same rule the files connector uses.
    """
    clean = str(upload_id or "")
    if not clean or not _ID.match(clean) or ".." in clean:
        return None

    root = upload_dir()
    full = (root / clean).resolve()
    try:
        full.relative_to(root.resolve())
    except ValueError:
        return None
    if not full.is_file():
        return None

    # The timestamp prefix is ours, not part of the user's own filename.
    return {"id": clean, "path": str(full), "name": re.sub(r"^[0-9a-f]+-", "", clean),
            "size": full.stat().st_size}


def list_uploads() -> list[dict[str, Any]]:
    try:
        files = sorted(upload_dir().iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    return [{"id": p.name, "name": re.sub(r"^[0-9a-f]+-", "", p.name),
             "size": p.stat().st_size} for p in files if p.is_file()]
