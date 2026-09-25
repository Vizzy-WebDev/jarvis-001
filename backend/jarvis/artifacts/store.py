"""Files Jarvis produced, and the check that they are real before they are kept.

**A file that cannot be opened is deleted, not recorded.** The verification is
mechanical: an Office document is re-opened with this project's own,
independently written reader, and if that fails the file goes and the caller is
told it failed. Keeping a broken file and marking it verified — which is what
happens when the check runs but its result is never stored — is worse than not
checking, because the record then asserts something false.

**An artifact belongs to the conversation that made it.** `session_id` is the
session the making turn ran in; `conversation_id` is the chat "Open in Chat"
goes back to. They differ on purpose — a specialist runs under its own session
(`agent:<id>:<conversation>`) and a background job has no chat at all — so the
conversation is resolved once, at creation, by `conversation_for()`.

Artifacts live under the data directory, so a test with a scratch data directory
cannot write into the user's real one. That is deliberate: a store that resolves
paths relative to its own source file writes into the real folder during scratch runs.
"""

from __future__ import annotations

import logging
import re
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..db import get_db
from ..jscompat import now_iso
from ..store import data_dir

logger = logging.getLogger(__name__)

VERIFIABLE = {".docx", ".xlsx", ".pptx"}
MAX_FILENAME = 80
MAX_TITLE = 120
#: Where a writer builds a file before `keep()` takes it. `keep()` removes the
#: (then empty) folder, so making an artifact leaves nothing behind in the temp dir.
STAGING_PREFIX = "jarvis-artifact-"
#: The same shape `agents/runner.py::session_for` builds. Read here as plain text
#: rather than imported: the agents package is far above this store.
_AGENT_SESSION = re.compile(r"^agent:[^:]+:(?P<conversation>.+)$")


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
    conversation_id: str | None = None
    title: str | None = None

    @property
    def path(self) -> Path:
        """Derived from the id, never from the name. A name comes from a model
        or a user; a path built from one is a path someone else chooses."""
        return artifacts_dir() / f"{self.id}{Path(self.name).suffix.lower()}"

    @property
    def kind(self) -> str:
        return kind_for(self.name)

    @property
    def display_title(self) -> str:
        return self.title or self.name

    def as_result(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "title": self.display_title,
                "kind": self.kind, "mimeType": self.mime_type,
                "size": self.size, "verified": self.verified,
                "createdAt": self.created_at, "conversationId": self.conversation_id,
                "url": f"/api/artifacts/{self.id}",
                **({"note": self.detail} if self.detail else {})}


def artifacts_dir() -> Path:
    """Under the data directory, so a scratch run cannot touch the real one."""
    path = Path(data_dir()) / "artifacts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def staging_path(name: str) -> Path:
    """A fresh place to build `name` before handing it to `keep()`."""
    return Path(tempfile.mkdtemp(prefix=STAGING_PREFIX)) / name


def discard_staging(path: Path) -> None:
    """Remove a staging file and its folder — for a build that never reached `keep()`."""
    path.unlink(missing_ok=True)
    _tidy(path.parent)


def _tidy(folder: Path) -> None:
    """Remove a staging folder once it is empty. Only ever one this module named."""
    if folder.name.startswith(STAGING_PREFIX):
        try:
            folder.rmdir()
        except OSError:
            pass  # not empty, or already gone — never worth failing a save over


def safe_name(name: str, default: str = "artifact") -> str:
    """A filename with nothing in it that could steer where it lands, or what a
    response header says. CR and LF are stripped too: an unsanitised filename in
    a Content-Disposition header injects response headers."""
    cleaned = re.sub(r"[\r\n\x00]", "", str(name or ""))
    cleaned = re.sub(r"[^A-Za-z0-9._ -]", "_", cleaned).strip(" .") or default
    return cleaned[:MAX_FILENAME]


def safe_title(title: str | None) -> str | None:
    """A display title: one line, no control characters, bounded."""
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", str(title or "")).strip()
    return cleaned[:MAX_TITLE] or None


MIME_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt": "text/plain", ".md": "text/markdown", ".csv": "text/csv",
    ".json": "application/json", ".html": "text/html", ".svg": "image/svg+xml",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".webp": "image/webp", ".pdf": "application/pdf",
    # Voice-overs from `tools/narrate_to_file.py`.
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg", ".opus": "audio/opus",
    ".aac": "audio/aac", ".flac": "audio/flac",
}


def mime_for(name: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix in MIME_TYPES:
        return MIME_TYPES[suffix]
    # Source code and other plain text is served as text, never as something a
    # browser might decide to run: the download route also sends `nosniff`.
    return "text/plain" if suffix in CODE_EXTENSIONS or suffix in TEXT_EXTENSIONS \
        else "application/octet-stream"


#: One category per extension: what the viewer does with it and what the
#: Artifacts page filters by. Anything not named is "other" (download to open).
CODE_EXTENSIONS = frozenset({
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".css", ".scss", ".sql", ".sh",
    ".bash", ".zsh", ".rb", ".go", ".rs", ".java", ".kt", ".c", ".h", ".cpp", ".hpp",
    ".cs", ".php", ".swift", ".r", ".lua", ".pl", ".dart", ".scala", ".vue", ".svelte",
    ".toml", ".ini", ".cfg", ".gradle", ".ipynb",
})
TEXT_EXTENSIONS = frozenset({".txt", ".log", ".rst", ".tex", ".srt", ".vtt"})
DATA_EXTENSIONS = frozenset({".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".xml",
                             ".ics", ".geojson"})
_KINDS: dict[str, frozenset[str]] = {
    "document": frozenset({".docx"}),
    "spreadsheet": frozenset({".xlsx"}),
    "presentation": frozenset({".pptx"}),
    "pdf": frozenset({".pdf"}),
    "markdown": frozenset({".md", ".markdown"}),
    "web": frozenset({".html", ".htm"}),
    "image": frozenset({".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp"}),
    "audio": frozenset({".mp3", ".wav", ".ogg", ".opus", ".aac", ".flac"}),
    "data": DATA_EXTENSIONS,
    "code": CODE_EXTENSIONS,
    "text": TEXT_EXTENSIONS,
}
KINDS = (*_KINDS, "other")


def kind_for(name: str) -> str:
    suffix = Path(str(name or "")).suffix.lower()
    for kind, extensions in _KINDS.items():
        if suffix in extensions:
            return kind
    return "other"


def conversation_for(session_id: str | None) -> str | None:
    """The chat a turn in `session_id` belongs to, or None when it has none.

    A conversation's own turns run under its id; a specialist's under
    `agent:<id>:<conversation>`; anything else (a background job, a scheduled
    task, a specialist asked with no conversation — `agent:<id>:solo`) has no
    chat to go back to. Only a conversation that really exists counts.
    """
    if not session_id:
        return None
    candidate = str(session_id)
    match = _AGENT_SESSION.match(candidate)
    if match:
        candidate = match.group("conversation")
    row = get_db().execute("SELECT 1 FROM conversations WHERE id = ?", (candidate,)).fetchone()
    return candidate if row else None


def _record(artifact: Artifact) -> None:
    get_db().execute(
        "INSERT INTO artifacts (id, name, mime_type, size, session_id, created_at, "
        "verified, verification_detail, conversation_id, title) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (artifact.id, artifact.name, artifact.mime_type, artifact.size,
         artifact.session_id, artifact.created_at,
         None if artifact.verified is None else int(artifact.verified), artifact.detail,
         artifact.conversation_id, artifact.title))


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


def keep(source: Path, *, name: str | None = None, session_id: str | None = None,
         conversation_id: str | None = None, title: str | None = None) -> Artifact:
    """Verify, then keep or delete. Returns the artifact; raises if it failed.

    The kept file is renamed to its id: what the user sees is the name, what the
    filesystem holds is an id, and the two cannot be made to disagree in a way
    that escapes the artifacts directory. `conversation_id` defaults to the one
    `session_id` belongs to.
    """
    verified, why = verify(source)
    if verified is False:
        # Deleted immediately rather than left behind pretending to be real.
        discard_staging(source)
        raise ValueError(f"I produced a file that doesn't open, so I've thrown it away. {why}")

    display = safe_name(name or source.name)
    artifact = Artifact(id=f"art_{uuid.uuid4().hex[:12]}", name=display,
                        mime_type=mime_for(display), size=source.stat().st_size,
                        verified=verified, created_at=now_iso(), session_id=session_id,
                        detail=why,
                        conversation_id=conversation_id or conversation_for(session_id),
                        title=safe_title(title))
    source.replace(artifact.path)
    _tidy(source.parent)
    _record(artifact)
    return artifact


def _from_row(row: Any) -> Artifact:
    return Artifact(id=row["id"], name=row["name"], mime_type=row["mime_type"],
                    size=row["size"] or 0, session_id=row["session_id"],
                    verified=None if row["verified"] is None else bool(row["verified"]),
                    created_at=row["created_at"], detail=row["verification_detail"],
                    conversation_id=row["conversation_id"], title=row["title"])


def get(artifact_id: str) -> Artifact | None:
    row = get_db().execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
    return _from_row(row) if row else None


def recent(limit: int = 20) -> list[Artifact]:
    return list_page(limit=limit)[0]


def _like(text: str) -> str:
    return "%" + re.sub(r"([\\%_])", r"\\\1", text) + "%"


def list_page(*, limit: int = 50, before: str | None = None, q: str | None = None,
              kind: str | None = None) -> tuple[list[Artifact], str | None]:
    """Newest first, one page at a time. Returns (artifacts, cursor for the next page).

    The cursor is `<created_at>|<id>` of the last row shown: two artifacts made in
    the same millisecond still page correctly, because the id breaks the tie.
    """
    limit = max(1, min(int(limit or 50), 200))
    where: list[str] = []
    args: list[Any] = []
    if before:
        stamp, _, last_id = before.partition("|")
        where.append("(created_at < ? OR (created_at = ? AND id < ?))")
        args += [stamp, stamp, last_id]
    if q and q.strip():
        where.append("(name LIKE ? ESCAPE '\\' OR COALESCE(title, '') LIKE ? ESCAPE '\\')")
        args += [_like(q.strip()), _like(q.strip())]
    if kind and kind in KINDS:
        known = sorted({ext for exts in _KINDS.values() for ext in exts})
        wanted = sorted(_KINDS.get(kind, frozenset()))
        if kind == "other":
            where.append("NOT (" + " OR ".join("lower(name) LIKE ?" for _ in known) + ")")
            args += ["%" + ext for ext in known]
        else:
            where.append("(" + " OR ".join("lower(name) LIKE ?" for _ in wanted) + ")")
            args += ["%" + ext for ext in wanted]
    sql = "SELECT * FROM artifacts"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    rows = get_db().execute(sql, (*args, limit + 1)).fetchall()
    page = [_from_row(r) for r in rows[:limit]]
    cursor = f"{page[-1].created_at}|{page[-1].id}" if len(rows) > limit and page else None
    return page, cursor


def delete(artifact_id: str) -> bool:
    """Remove the file and its record. False when there was no such artifact.

    Only the person deletes: no capability calls this. The file goes first, so a
    failure to remove it leaves the record (still listed, still retryable) rather
    than a record-less file nobody can find again.
    """
    artifact = get(artifact_id)
    if artifact is None:
        return False
    artifact.path.unlink(missing_ok=True)
    get_db().execute("DELETE FROM artifacts WHERE id = ?", (artifact_id,))
    return True
