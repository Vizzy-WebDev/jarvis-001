"""Reading, writing, listing and moving files — inside an allowlist.

Jarvis's own ability rather than something the user sets up on a screen. The
allowlist starts empty and grows only through conversation: a file operation
outside it fails with a plain error, the model asks, and only after an explicit
yes does `allow_folder` remember it.

**Every path is resolved and checked BEFORE any filesystem call.** A relative
path and a `..` segment both look like they stay where they are and do not, and
a symlink is only caught by resolving it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

MAX_LIST_ENTRIES = 200
#: Plenty for a text file, and not an accidental read of something huge.
MAX_READ_BYTES = 512 * 1024


class NotAllowed(PermissionError):
    """Outside every allowed folder. The message is written for the user."""


def is_allowed(target: Path | str, allowed_folders: list[str] | None) -> bool:
    resolved = Path(target).expanduser().resolve()
    for folder in allowed_folders or []:
        base = Path(folder).expanduser().resolve()
        if resolved == base or base in resolved.parents:
            return True
    return False


def require_allowed(target: Path | str, allowed_folders: list[str] | None) -> Path:
    if not allowed_folders:
        raise NotAllowed("No folders are allowed yet — ask the user for permission to a "
                         "specific folder first, then use allow_folder.")
    if not is_allowed(target, allowed_folders):
        raise NotAllowed(f'"{target}" is outside every allowed folder — nothing was touched.')
    return Path(target).expanduser().resolve()


def list_files(path: str, allowed: list[str] | None) -> dict[str, Any]:
    resolved = require_allowed(path, allowed)
    if not resolved.is_dir():
        raise NotADirectoryError(f'"{path}" is not a folder.')
    entries = sorted(resolved.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    listed = [{"name": p.name, "kind": "folder" if p.is_dir() else "file"}
              for p in entries[:MAX_LIST_ENTRIES]]
    return {"ok": True, "path": str(resolved), "entries": listed,
            "truncated": len(entries) > len(listed)}


def read_file(path: str, allowed: list[str] | None) -> dict[str, Any]:
    resolved = require_allowed(path, allowed)
    if not resolved.is_file():
        raise FileNotFoundError(f'"{path}" is not a file.')
    size = resolved.stat().st_size
    if size > MAX_READ_BYTES:
        raise ValueError(f'"{path}" is {round(size / 1024)}KB — too big to read this way.')
    return {"ok": True, "path": str(resolved),
            "content": resolved.read_text(encoding="utf-8", errors="replace")}


def write_file(path: str, content: str, allowed: list[str] | None) -> dict[str, Any]:
    resolved = require_allowed(path, allowed)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(str(content or ""), encoding="utf-8")
    return {"ok": True, "path": str(resolved), "bytes": len(str(content or "").encode())}


def move_file(source: str, destination: str, allowed: list[str] | None) -> dict[str, Any]:
    # BOTH ends are checked: moving a file out of an allowed folder into an
    # unallowed one is still reaching somewhere it may not.
    from_path = require_allowed(source, allowed)
    to_path = require_allowed(destination, allowed)
    if not from_path.exists():
        raise FileNotFoundError(f'"{source}" is not there.')
    to_path.parent.mkdir(parents=True, exist_ok=True)
    from_path.replace(to_path)
    return {"ok": True, "from": str(from_path), "to": str(to_path)}


#: The tools this connector offers. Names are unprefixed: there is only one
#: files connector, so nothing can collide with it.
TOOLS: list[dict[str, Any]] = [
    {"name": "list_files", "description": "List what is in a folder on this computer.",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string", "description": "The folder's full path."}},
         "required": ["path"]}},
    {"name": "read_file", "description": "Read a text file from this computer.",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string", "description": "The file's full path."}},
         "required": ["path"]}},
    {"name": "write_file",
     "description": "Write a text file on this computer, replacing what is there.",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string", "description": "The file's full path."},
         "content": {"type": "string", "description": "What to write."}},
         "required": ["path", "content"]}},
    {"name": "move_file", "description": "Move or rename a file on this computer.",
     "parameters": {"type": "object", "properties": {
         "from": {"type": "string", "description": "The file's current full path."},
         "to": {"type": "string", "description": "Where it should end up."}},
         "required": ["from", "to"]}},
]


def dispatch(name: str, args: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    allowed = (config or {}).get("allowedFolders") or []
    if name == "list_files":
        return list_files(args.get("path", ""), allowed)
    if name == "read_file":
        return read_file(args.get("path", ""), allowed)
    if name == "write_file":
        return write_file(args.get("path", ""), args.get("content", ""), allowed)
    if name == "move_file":
        return move_file(args.get("from", ""), args.get("to", ""), allowed)
    raise KeyError(f"{name} is not a file operation.")
