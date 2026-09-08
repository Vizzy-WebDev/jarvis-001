"""Saved connectors: one record per way of reaching something.

`files` is a singleton — there is one folder allowlist, not a list of them — and
is created on demand rather than added by the user. `mcp`, `api` and `cli` are
the three the user actually connects things through.

A leaf: the JSON store and the clock.
"""

from __future__ import annotations

import uuid
from typing import Any

from ..jscompat import now_iso
from ..store import read_json, write_json

FILE = "connectors"

#: Jarvis's own abilities, one of each, created when first needed. The label is
#: what a person sees, so it is written as a person would write it.
SINGLETON_TYPES = ("files", "browser")
SINGLETON_LABELS = {"files": "Files", "browser": "Browser"}
#: What a user actually connects.
USER_TYPES = ("mcp", "api", "cli")


def _load() -> dict[str, Any]:
    data = read_json(FILE, {"connectors": []})
    if not isinstance(data, dict) or not isinstance(data.get("connectors"), list):
        return {"connectors": []}
    return data


def _save(data: dict[str, Any]) -> None:
    write_json(FILE, data)


def list_connectors(kind: str | None = None) -> list[dict[str, Any]]:
    connectors = _load()["connectors"]
    return [c for c in connectors if kind is None or c.get("type") == kind]


def get_connector(connector_id: str) -> dict[str, Any] | None:
    return next((c for c in _load()["connectors"] if c.get("id") == connector_id), None)


def add_connector(*, type: str, label: str, config: dict[str, Any] | None = None,
                  enabled: bool = True) -> dict[str, Any]:
    if type not in SINGLETON_TYPES + USER_TYPES:
        raise ValueError(f'"{type}" is not a kind of connector this understands.')
    connector = {
        "id": f"c{uuid.uuid4().hex[:12]}",
        "type": type,
        "label": label or type,
        "enabled": enabled,
        "config": config or {},
        # "untested" rather than "unknown": nothing has been tried yet, which is
        # a different thing from having tried and not been able to tell.
        "status": {"state": "untested", "checkedAt": None, "detail": None},
        "createdAt": now_iso(),
        "updatedAt": now_iso(),
    }
    data = _load()
    data["connectors"].append(connector)
    _save(data)
    return connector


def update_connector(connector_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    """Patch a connector. `config` merges ONE LEVEL DEEP rather than replacing
    outright — patching just `toolPermissions`, or just `connectFlow`, should
    never require resending the rest of `config` (a stored secret ref, a cached
    tool list) alongside it. Every OTHER top-level key still replaces plainly.
    Every existing caller already builds and passes the full merged `config` by
    hand, so this is additive: it changes nothing for them and is what lets a
    narrower patch (OAuth's own `connectFlow` updates) be written safely."""
    data = _load()
    for index, connector in enumerate(data["connectors"]):
        if connector.get("id") != connector_id:
            continue
        merged = {**connector, **patch, "id": connector_id, "updatedAt": now_iso()}
        if "config" in patch:
            merged["config"] = {**(connector.get("config") or {}), **(patch["config"] or {})}
        data["connectors"][index] = merged
        _save(data)
        return merged
    return None


def delete_connector(connector_id: str) -> None:
    data = _load()
    data["connectors"] = [c for c in data["connectors"] if c.get("id") != connector_id]
    _save(data)


def get_or_create_singleton(kind: str) -> dict[str, Any]:
    """The one `files` connector, made when something first needs it.

    On demand rather than at install time: an empty allowlist and no record at
    all mean the same thing, and the second is one less row to explain.
    """
    if kind not in SINGLETON_TYPES:
        raise ValueError(f'"{kind}" is not a singleton connector.')
    existing = next((c for c in _load()["connectors"] if c.get("type") == kind), None)
    if existing is not None:
        return existing
    return add_connector(type=kind, label=SINGLETON_LABELS.get(kind, kind),
                         config={"allowedFolders": []})


def tool_permission(connector: dict[str, Any], tool_name: str) -> str:
    """'allow' | 'ask' | 'deny' for one tool of one connector.

    Standing permission and runtime confirmation are two different things, kept
    apart deliberately: this answers "is Jarvis allowed to use this at all",
    which is the user's call per tool. Whether an allowed tool still pauses to
    ask right now is the risk classifier's business, and neither can suppress
    the other.
    """
    permissions = (connector.get("config") or {}).get("toolPermissions") or {}
    value = permissions.get(tool_name)
    return value if value in ("allow", "ask", "deny") else "allow"


def set_tool_permission(connector_id: str, tool_name: str, permission: str) -> dict[str, Any]:
    if permission not in ("allow", "ask", "deny"):
        raise ValueError('A permission is "allow", "ask" or "deny".')
    connector = get_connector(connector_id)
    if connector is None:
        raise KeyError("That connector no longer exists.")
    config = dict(connector.get("config") or {})
    permissions = dict(config.get("toolPermissions") or {})
    permissions[tool_name] = permission
    config["toolPermissions"] = permissions
    return update_connector(connector_id, {"config": config})  # type: ignore[return-value]
