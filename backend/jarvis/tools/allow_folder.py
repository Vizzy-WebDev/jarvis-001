"""Remembering that the user said yes to a folder.

The file allowlist starts empty and grows only through conversation: an
operation outside it fails with a plain error, the model asks, and this records
the answer. That shape is deliberate — there is no settings screen where someone
grants blanket access to their disk in advance and forgets they did.

Confirmed like anything else that changes what Jarvis may reach, and read back
with the real path, because "can I have access to that folder?" and "can I have
access to C:\\Users" are very different questions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..capabilities import CapabilitySpec, Risk


def _run(registry: Any, path: str = "") -> dict[str, Any]:
    from ..connectors import store
    from ..connectors.capabilities import sync

    target = Path(str(path or "").strip()).expanduser()
    if not str(path or "").strip():
        return {"ok": False, "error": "I didn't catch which folder."}
    if not target.exists():
        return {"ok": False, "error": f'"{path}" isn\'t a folder on this computer.'}
    if not target.is_dir():
        return {"ok": False, "error": f'"{path}" is a file, not a folder.'}

    connector = store.get_or_create_singleton("files")
    config = dict(connector.get("config") or {})
    folders = list(config.get("allowedFolders") or [])
    resolved = str(target.resolve())
    if resolved in folders:
        return {"ok": True, "path": resolved, "alreadyAllowed": True,
                "note": "That folder was already allowed."}

    folders.append(resolved)
    config["allowedFolders"] = folders
    store.update_connector(connector["id"], {"config": config})

    # The file tools only exist once there is somewhere to use them.
    sync(registry)
    return {"ok": True, "path": resolved,
            "note": "Files in that folder and below it can be read and written from now on."}


def build(registry: Any) -> list[CapabilitySpec]:
    """Built with the registry passed in: a tool must never reach for the shared
    one, and this needs it to make the file tools appear."""
    return [CapabilitySpec(
        id="builtin.allow_folder",
        name="allow_folder",
        description=("Remember that the user has given permission to work with files in "
                     "one folder. Only call it after actually asking them about that "
                     "specific folder and getting a yes — never to pre-empt a refusal, "
                     "and never on a folder they didn't name."),
        input_schema={"type": "object", "properties": {
            "path": {"type": "string",
                     "description": "The folder's full path, exactly as they gave it."}},
            "required": ["path"]},
        risk=Risk.MEDIUM,
        handler=lambda **args: _run(registry, **args),
        timeout_s=10.0,
        summarize=lambda args: (f'Allow reading and writing files in "{args.get("path")}" '
                                f"and everything below it?"),
    )]
