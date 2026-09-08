"""Where screenshots and screen recordings live, and when they expire.

One module for both, because they differ only in extension and retention
numbers — the original had two near-identical files and both carried the same
bug.

**That bug is the reason this file's paths are built the way they are.** Both
originals derived their directory from the source file's own location and
honoured only their own dedicated environment variable, so a test run isolated
the documented way — `JARVIS_DATA_DIR` pointed at a scratch directory — still
wrote screenshots and videos into the user's REAL data folder. It was observed
happening during this migration (`docs/migration/findings.md`, finding 1). Here
the directory comes from `store.data_dir()` like every other data path, and the
dedicated variable is only an explicit override on top.

Ids are opaque and generated here. Nothing a model wrote ever becomes part of a
filename: these files are served over HTTP by id, and a name is exactly the sort
of thing that turns into a path traversal or an injected response header.
"""

from __future__ import annotations

import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from ..store import data_dir
from .safety import get_safety_config

#: A stored capture's id: a timestamp for ordering, a short random suffix so two
#: in the same second cannot collide, and nothing else.
_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[a-f0-9]{6}$")


@dataclass(frozen=True)
class Kind:
    name: str
    suffix: str
    mime_type: str
    directory: str
    env_override: str
    retention_key: str


SCREENSHOT = Kind(name="screenshot", suffix=".png", mime_type="image/png",
                  directory="screenshots", env_override="JARVIS_SCREENSHOTS_DIR",
                  retention_key="screenshotRetention")
RECORDING = Kind(name="recording", suffix=".mp4", mime_type="video/mp4",
                 directory="recordings", env_override="JARVIS_RECORDINGS_DIR",
                 retention_key="recordingRetention")

KINDS = {SCREENSHOT.name: SCREENSHOT, RECORDING.name: RECORDING}


@dataclass(frozen=True)
class Capture:
    id: str
    kind: str
    path: Path
    taken_at: float
    size: int

    def as_dict(self) -> dict[str, object]:
        # `file` and `takenAt` are the shape the recorded API already lists; the
        # id and the url are ours, and additive.
        return {"file": f"{self.id}{KINDS[self.kind].suffix}", "id": self.id,
                "takenAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.taken_at)),
                "bytes": self.size, "url": url_for(self.kind, self.id)}


def directory_for(kind: Kind) -> Path:
    """The folder, honouring JARVIS_DATA_DIR — never a path relative to this
    file. See the module docstring for the bug that rule exists to prevent."""
    override = os.environ.get(kind.env_override)
    path = Path(override) if override else data_dir() / kind.directory
    path.mkdir(parents=True, exist_ok=True)
    return path


#: The recorded API serves these under /api/control/, one path per kind. Kept
#: rather than invented anew: the contract fixtures pin these paths, and a
#: gratuitously different URL is a divergence with nothing behind it.
ROUTE_SEGMENT = {SCREENSHOT.name: "screenshots", RECORDING.name: "recordings"}


def url_for(kind: str, capture_id: str) -> str:
    return f"/api/control/{ROUTE_SEGMENT.get(kind, kind)}/{capture_id}"


def new_id() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"


def save(kind: Kind, data: bytes) -> Capture:
    """Write one capture, making room for it first."""
    prune(kind, room_for=1)
    capture_id = new_id()
    path = directory_for(kind) / f"{capture_id}{kind.suffix}"
    path.write_bytes(data)
    return Capture(id=capture_id, kind=kind.name, path=path,
                   taken_at=path.stat().st_mtime, size=len(data))


def reserve(kind: Kind) -> tuple[str, Path]:
    """An id and a path to write to, for something that produces the file
    itself — a recording is written by ffmpeg, not handed to us as bytes."""
    prune(kind, room_for=1)
    capture_id = new_id()
    return capture_id, directory_for(kind) / f"{capture_id}{kind.suffix}"


def get(kind_name: str, capture_id: str) -> Capture | None:
    kind = KINDS.get(kind_name)
    if kind is None or not _ID.match(str(capture_id or "")):
        return None
    path = directory_for(kind) / f"{capture_id}{kind.suffix}"
    # The id shape above already forbids a separator, but the resolved path is
    # checked against the folder anyway: one cheap check, and the alternative is
    # trusting a regex to be the only thing standing between a URL and the disk.
    try:
        resolved = path.resolve()
        resolved.relative_to(directory_for(kind).resolve())
    except (OSError, ValueError):
        return None
    if not resolved.exists():
        return None
    stat = resolved.stat()
    return Capture(id=capture_id, kind=kind.name, path=resolved,
                   taken_at=stat.st_mtime, size=stat.st_size)


def recent(kind: Kind, limit: int = 50) -> list[Capture]:
    """Newest first."""
    found = []
    for path in directory_for(kind).glob(f"*{kind.suffix}"):
        if not _ID.match(path.stem):
            continue
        stat = path.stat()
        found.append(Capture(id=path.stem, kind=kind.name, path=path,
                             taken_at=stat.st_mtime, size=stat.st_size))
    found.sort(key=lambda c: c.taken_at, reverse=True)
    return found[:limit]


def prune(kind: Kind, *, room_for: int = 0) -> int:
    """Delete anything past the retention count or older than the age limit.

    Both come from the safety config, so retention sits with the rest of what
    the user can already see and change.

    `room_for` is what a caller is about to add. Without it the count settles at
    one MORE than the limit — prune, then write, then prune again next time —
    which is the sort of off-by-one a retention setting is never expected to
    have. The file about to be written is never itself pruned, so a screenshot
    someone just asked for is still there to look at even at a retention of zero.
    """
    retention = get_safety_config().get(kind.retention_key) or {}
    max_count = max(0, int(retention.get("maxCount", 50)) - max(0, room_for))
    max_age_hours = float(retention.get("maxAgeHours", 24))
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for index, capture in enumerate(recent(kind, limit=10_000)):
        if index >= max_count or capture.taken_at < cutoff:
            try:
                capture.path.unlink()
                removed += 1
            except OSError:
                pass                          # already gone is the goal anyway
    return removed


def clear(kind: Kind) -> int:
    removed = 0
    for capture in recent(kind, limit=10_000):
        try:
            capture.path.unlink()
            removed += 1
        except OSError:
            pass
    return removed
