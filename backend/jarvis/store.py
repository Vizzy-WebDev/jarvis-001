"""Reading/writing the structured JSON files under data/ (models, tasks,
briefing config, ...).

A faithful port of server/store.js. Kept dependency-free on purpose, matching
the original. Writes are atomic (temp file + rename) so a crash or power loss
mid-write can never leave a half-written, corrupt JSON file behind.

FORMAT COMPATIBILITY IS THE POINT OF THIS FILE. The Node app and this one must
be able to read each other's output byte for byte during the migration, so the
serialisation here deliberately mirrors `JSON.stringify(value, null, 2)`:
two-space indent, `": "` between key and value, no trailing newline, and no
\\uXXXX escaping of non-ASCII (JSON.stringify emits raw UTF-8; ensure_ascii
would not).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

# Overridable so a test server can point at a completely separate, disposable
# data directory instead of the user's real one — the same reasoning as the
# PORT override (see root CLAUDE.md): never touch a copy the user already has
# open while testing changes.
_DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"


def _data_dir_path() -> Path:
    # Read the env var on every call rather than caching at import time: tests
    # set JARVIS_DATA_DIR after this module is already imported, and a cached
    # value would silently send them at the user's real data directory.
    override = os.environ.get("JARVIS_DATA_DIR")
    return Path(override) if override else _DEFAULT_DATA_DIR


def _ensure_data_dir() -> Path:
    d = _data_dir_path()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _file_path(name: str) -> Path:
    return _data_dir_path() / f"{name}.json"


# In-memory only, per-process, reset on restart — a THIS-PROCESS record of "I
# legitimately wrote this exact content just now", never persisted. This is
# what lets the ops config-integrity check tell apart a change this app itself
# made through a real route from one that happened some other way. Records the
# WRITTEN CONTENT's own hash, not just a timestamp: a time-window heuristic was
# tried in the original and found genuinely wrong — a legitimate write's
# timestamp stays "recent" long enough to wrongly excuse a LATER, unrelated
# external change landing inside the same window.
_last_writes: dict[str, dict[str, Any]] = {}


def last_write_at(name: str) -> dict[str, Any] | None:
    """`{ts, hash}` of THIS process's last write_json() for data/<name>.json."""
    return _last_writes.get(name)


def serialize(value: Any) -> str:
    """The exact byte-for-byte equivalent of `JSON.stringify(value, null, 2)`."""
    return json.dumps(value, indent=2, ensure_ascii=False)


def read_json(name: str, fallback: Any = None) -> Any:
    """Reads data/<name>.json, returning `fallback` if missing or corrupt."""
    try:
        return json.loads(_file_path(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback


def write_json(name: str, value: Any) -> None:
    """Atomically writes `value` to data/<name>.json (temp file + rename)."""
    _ensure_data_dir()
    target = _file_path(name)
    contents = serialize(value)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.{int(time.time() * 1000)}.tmp")
    tmp.write_text(contents, encoding="utf-8")
    # os.replace is atomic on both POSIX and Windows, unlike os.rename which
    # raises on Windows if the destination already exists.
    os.replace(tmp, target)
    _last_writes[name] = {
        "ts": int(time.time() * 1000),
        "hash": hashlib.sha256(contents.encode("utf-8")).hexdigest(),
    }


def exists(name: str) -> bool:
    """True if data/<name>.json exists on disk."""
    return _file_path(name).exists()


def data_file_path(name: str) -> Path:
    """The absolute path to data/<name>.json, for a caller that must hand the
    path itself to something outside this module. Ensures the data dir exists
    first, so such a caller needs no mkdir logic of its own."""
    _ensure_data_dir()
    return _file_path(name)


def data_dir() -> Path:
    """The data directory itself, for a caller needing its own subdirectory
    there rather than a single JSON file. Respects JARVIS_DATA_DIR exactly like
    every other function here — a module that hardcodes a path relative to its
    own source file bypasses test isolation entirely (a real bug found the hard
    way in the original: a connector wrote a 145MB browser profile into the real
    data/ directory during a scratch test run)."""
    return _ensure_data_dir()


def reset_for_tests() -> None:
    """Test-only: forget this process's record of what it recently wrote.

    Production code never calls this — the record lives for the process lifetime,
    exactly as it does in the Node original. Tests need it because they share one
    process across many scratch data directories, and a hash left over from a
    previous directory could wrongly "explain" a file in the next one, which is
    precisely the confusion the content-hash design exists to prevent.
    """
    _last_writes.clear()
