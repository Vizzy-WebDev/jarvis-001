"""Files Jarvis produced, and the check that they are real before they are kept.

**A file that cannot be opened is deleted, not recorded.** The verification is
mechanical: an Office document is re-opened with this project's own,
independently written reader, and if that fails the file goes and the caller is
told it failed. Keeping a broken file and marking it verified — which is what
happens when the check runs but its result is never stored — is worse than not
checking, because the record then asserts something false.

Artifacts live under the data directory, so a test with a scratch data directory
cannot write into the user's real one. That is deliberate: the original had two
stores that resolved paths relative to their own source file and wrote into the
real folder during scratch runs.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..db import get_db
from ..jscompat import now_iso
from ..store import data_dir

logger = logging.getLogger(__name__)

VERIFIABLE = {".docx", ".xlsx"}
MAX_FILENAME = 80


@dataclass
class Artifact:
    id: str
    name: str
    mime_type: str
    size: int
    verified: bool | None
    created_at: str
    session_id: str | None = None
    detail: str | None = None

    @property
    def path(self) -> Path:
        """Derived from the id, never from the name. A name comes from a model
        or a user; a path built from one is a path someone else chooses."""
        return artifacts_dir() / f"{self.id}{Path(self.name).suffix.lower()}"

    def as_result(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "mimeType": self.mime_type,
                "size": self.size, "verified": self.verified,
                "url": f"/api/artifacts/{self.id}",
                **({"note": self.detail} if self.detail else {})}


def artifacts_dir() -> Path:
    """Under the data directory, so a scratch run cannot touch the real one."""
    path = Path(data_dir()) / "artifacts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_name(name: str, default: str = "artifact") -> str:
    """A filename with nothing in it that could steer where it lands, or what a
    response header says. CR and LF are stripped too: an unsanitised filename in
    a Content-Disposition header injects response headers."""
    cleaned = re.sub(r"[\r\n\x00]", "", str(name or ""))
    cleaned = re.sub(r"[^A-Za-z0-9._ -]", "_", cleaned).strip(" .") or default
    return cleaned[:MAX_FILENAME]


MIME_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".txt": "text/plain", ".md": "text/markdown", ".csv": "text/csv",
    ".json": "application/json", ".html": "text/html", ".svg": "image/svg+xml",
    ".png": "image/png", ".pdf": "application/pdf",
}


def mime_for(name: str) -> str:
    return MIME_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")


def _record(artifact: Artifact) -> None:
    get_db().execute(
        "INSERT INTO artifacts (id, name, mime_type, size, session_id, created_at, "
        "verified, verification_detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (artifact.id, artifact.name, artifact.mime_type, artifact.size,
         artifact.session_id, artifact.created_at,
         None if artifact.verified is None else int(artifact.verified), artifact.detail))


def verify(path: Path) -> tuple[bool | None, str | None]:
    """(verified, why). None means "not checkable", which is NOT the same as
    verified and must never be recorded as though it were."""
    suffix = path.suffix.lower()
    if suffix not in VERIFIABLE:
        return None, "Nothing here can re-open this format, so it has not been checked."
    from ..documents import read_document

    try:
        read_document(path)
    except Exception as err:  # noqa: BLE001
        return False, f"It could not be re-opened: {err}"
    return True, None


def keep(source: Path, *, name: str | None = None,
         session_id: str | None = None) -> Artifact:
    """Verify, then keep or delete. Returns the artifact; raises if it failed.

    The kept file is renamed to its id: what the user sees is the name, what the
    filesystem holds is an id, and the two cannot be made to disagree in a way
    that escapes the artifacts directory.
    """
    verified, why = verify(source)
    if verified is False:
        # Deleted immediately rather than left behind pretending to be real.
        source.unlink(missing_ok=True)
        raise ValueError(f"I produced a file that doesn't open, so I've thrown it away. {why}")

    display = safe_name(name or source.name)
    artifact = Artifact(id=f"art_{uuid.uuid4().hex[:12]}", name=display,
                        mime_type=mime_for(display), size=source.stat().st_size,
                        verified=verified, created_at=now_iso(), session_id=session_id,
                        detail=why)
    source.replace(artifact.path)
    _record(artifact)
    return artifact


def _from_row(row: Any) -> Artifact:
    return Artifact(id=row["id"], name=row["name"], mime_type=row["mime_type"],
                    size=row["size"] or 0, session_id=row["session_id"],
                    verified=None if row["verified"] is None else bool(row["verified"]),
                    created_at=row["created_at"], detail=row["verification_detail"])


def get(artifact_id: str) -> Artifact | None:
    row = get_db().execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
    return _from_row(row) if row else None


def recent(limit: int = 20) -> list[Artifact]:
    rows = get_db().execute(
        "SELECT * FROM artifacts ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [_from_row(r) for r in rows]
