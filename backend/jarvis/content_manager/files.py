"""The actual media of content items — videos, images, slides, audio, assets.

Stored under `data_dir()/content-media/`, named ONLY by a generated id plus a
suffix from a fixed table. No part of a caller's filename ever reaches the path,
and an id is matched against a fixed shape before it is looked up, so a request
cannot walk out of this directory.

A leaf: the database, the store helpers and `media.py`'s MIME table.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any, BinaryIO

from ..db import get_db
from ..jscompat import now_iso
from ..media import MIME_BY_SUFFIX, file_kind
from ..store import data_dir

#: Big enough for a long, high-quality video; bounded so a runaway request cannot
#: fill the disk.
MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024
_CHUNK = 1024 * 1024
_ID = re.compile(r"^cmf_[0-9a-f]{12}$")


class FileTooBig(ValueError):
    pass


def media_dir() -> Path:
    folder = data_dir() / "content-media"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _suffix(name: str) -> str:
    suffix = Path(name or "").suffix.lower()
    return suffix if suffix in MIME_BY_SUFFIX else ""


def _path(file_id: str, suffix: str) -> Path:
    return media_dir() / f"{file_id}{suffix}"


def save_stream(source: BinaryIO, name: str, *, item_id: str | None = None) -> dict[str, Any]:
    """Copy a readable stream into the store, refusing anything over the cap."""
    clean = (name or "file").replace("\r", "").replace("\n", "").replace("\x00", "")[:200]
    file_id = f"cmf_{uuid.uuid4().hex[:12]}"
    suffix = _suffix(clean)
    target = _path(file_id, suffix)
    size = 0
    try:
        with target.open("wb") as out:
            while True:
                chunk = source.read(_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise FileTooBig(f"“{clean}” is bigger than 2 GB, which is the most a single file can be.")
                out.write(chunk)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    if size == 0:
        target.unlink(missing_ok=True)
        raise ValueError(f"“{clean}” is empty.")
    mime = MIME_BY_SUFFIX.get(suffix, "application/octet-stream")
    get_db().execute(
        "INSERT INTO cm_files (id, item_id, name, mime, size, suffix, created_at) VALUES (?,?,?,?,?,?,?)",
        (file_id, item_id, clean, mime, size, suffix, now_iso()))
    return describe({"id": file_id, "name": clean, "mime": mime, "size": size, "suffix": suffix})


def save_path(path: Path, name: str, *, item_id: str | None = None) -> dict[str, Any]:
    with Path(path).open("rb") as source:
        return save_stream(source, name, item_id=item_id)


def describe(row: Any) -> dict[str, Any]:
    return {"fileId": row["id"], "name": row["name"], "mime": row["mime"], "size": row["size"],
            "kind": file_kind(f"x{row['suffix']}"), "url": f"/api/content-media/{row['id']}"}


def get(file_id: str) -> tuple[dict[str, Any], Path] | None:
    if not _ID.match(file_id or ""):
        return None
    row = get_db().execute("SELECT * FROM cm_files WHERE id = ?", (file_id,)).fetchone()
    if row is None:
        return None
    path = _path(row["id"], row["suffix"])
    return (describe(row), path) if path.is_file() else None


def lookup(file_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not file_ids:
        return {}
    marks = ",".join("?" * len(file_ids))
    rows = get_db().execute(f"SELECT * FROM cm_files WHERE id IN ({marks})", file_ids).fetchall()
    return {row["id"]: describe(row) for row in rows}


def claim_for_item(file_ids: list[str], item_id: str) -> None:
    """Tie files saved before their item existed to it. Only unowned files, or
    ones already this item's, can be claimed — never another item's."""
    for file_id in file_ids:
        get_db().execute("UPDATE cm_files SET item_id = ? WHERE id = ? AND (item_id IS NULL OR item_id = ?)",
                         (item_id, file_id, item_id))


def owned_by(file_ids: list[str], item_id: str) -> list[str]:
    found = lookup(file_ids)
    rows = get_db().execute("SELECT id FROM cm_files WHERE item_id = ?", (item_id,)).fetchall()
    mine = {r["id"] for r in rows}
    return [f for f in file_ids if f in found and f in mine]


def is_unowned(file_id: str) -> bool:
    row = get_db().execute("SELECT item_id FROM cm_files WHERE id = ?", (file_id,)).fetchone()
    return row is not None and row["item_id"] is None


def discard(file_ids: list[str]) -> None:
    """Remove files that never became part of an item (a submission that failed)."""
    for file_id in file_ids:
        row = get_db().execute("SELECT * FROM cm_files WHERE id = ? AND item_id IS NULL",
                               (file_id,)).fetchone()
        if row is None:
            continue
        _path(row["id"], row["suffix"]).unlink(missing_ok=True)
        get_db().execute("DELETE FROM cm_files WHERE id = ?", (file_id,))


def paths_for_item(item_id: str) -> list[Path]:
    rows = get_db().execute("SELECT id, suffix FROM cm_files WHERE item_id = ?", (item_id,)).fetchall()
    return [_path(r["id"], r["suffix"]) for r in rows]

