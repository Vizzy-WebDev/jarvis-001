"""A connection: one saved address + key, hosting one or more models.

Plain CRUD plus secret lifecycle — a leaf module, no app logic. Several models
discovered together share one connection rather than each duplicating the same
key.

Byte-compatible with the Node implementation's `data/connections.json`: the same
keys in the same order, so the two implementations can read each other's file
during the port.
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from typing import Any

from ..config import delete_secret, save_secret
from ..store import read_json, write_json

FILE = "connections"

#: Deleting a connection using one of these must NOT delete the secret —
#: GEMINI_API_KEY is also what turn-checking reads, regardless of which model is
#: chatting, so removing a migrated "Gemini" connection must not break it.
LEGACY_SECRET_REFS = frozenset({"gemini", "anthropic", "openai"})

# Read-modify-write over a shared file. The Node original is serialised by the
# event loop; uvicorn runs sync endpoints in a thread pool, so this needs a real
# lock or two concurrent adds lose one of themselves.
_lock = threading.RLock()


def _load() -> dict[str, Any]:
    data = read_json(FILE, {"connections": []})
    if not isinstance(data, dict) or not isinstance(data.get("connections"), list):
        return {"connections": []}
    return data


def _save(data: dict[str, Any]) -> None:
    write_json(FILE, data)


def list_connections() -> list[dict[str, Any]]:
    return _load()["connections"]


def get_connection(connection_id: str) -> dict[str, Any] | None:
    return next((c for c in list_connections() if c.get("id") == connection_id), None)


def _slug(seed: str, fallback: str) -> str:
    base = re.sub(r"(^-|-$)", "", re.sub(r"[^a-z0-9]+", "-", str(seed or fallback).lower().strip()))
    return base or fallback


def _make_id(seed: str, existing: list[dict[str, Any]]) -> str:
    base = _slug(seed, "connection")
    taken = {c.get("id") for c in existing}
    candidate, n = base, 2
    while candidate in taken:
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def default_label(adapter: str | None, base_url: str | None) -> str:
    if base_url:
        match = re.match(r"^[a-z]+://([^/]+)", base_url, re.I)
        return match.group(1) if match else base_url
    return adapter or "connection"


def add_connection(
    *,
    adapter: str,
    base_url: str | None = None,
    label: str | None = None,
    secret: str | None = None,
    secret_ref: str | None = None,
    connection_id: str | None = None,
    provider: str | None = None,
    kind: str | None = None,
    key_required: bool | None = None,
) -> dict[str, Any]:
    """Add a connection, saving its secret under a fresh ref unless one is given.

    `provider`, `kind` and `key_required` are captured HERE, once, as facts —
    never re-derived from the URL on every read. That is what makes a Custom
    address safe to add at all: the address alone does not say what is behind it.
    """
    if not adapter:
        raise ValueError("A connection needs an adapter.")
    with _lock:
        data = _load()
        final_label = label or default_label(adapter, base_url)
        if connection_id and any(c.get("id") == connection_id for c in data["connections"]):
            raise ValueError(f"Connection id already in use: {connection_id}")
        cid = connection_id or _make_id(final_label, data["connections"])
        ref = secret_ref or (f"conn_{cid}" if secret else None)

        connection = {
            "id": cid,
            "label": final_label,
            "adapter": adapter,
            "baseUrl": base_url or None,
            "secretRef": ref,
            "provider": provider or None,
            "kind": kind if kind is not None else None,
            "keyRequired": key_required if isinstance(key_required, bool) else None,
            "createdAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        }
        # Only save a fresh secret when we minted the ref ourselves; a passed-in
        # ref (the legacy migration path) already has its value stored.
        if secret and ref and not secret_ref:
            save_secret(ref, secret)

        data["connections"].append(connection)
        _save(data)
        return connection


def update_connection(connection_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        data = _load()
        index = next((i for i, c in enumerate(data["connections"]) if c.get("id") == connection_id), -1)
        if index == -1:
            raise KeyError(f"Unknown connection: {connection_id}")
        connection = dict(data["connections"][index])

        rest = dict(patch)
        secret = rest.pop("secret", None)
        if secret:
            ref = connection.get("secretRef") or f"conn_{connection['id']}"
            save_secret(ref, secret)
            connection["secretRef"] = ref
        connection.update(rest)
        data["connections"][index] = connection
        _save(data)
        return connection


def remove_connection(connection_id: str, *, keep_secret: bool = False) -> None:
    """Remove the row and (usually) its secret. Never touches a legacy ref.

    Does not touch models still pointing here — see the model registry's own
    cascading delete for that.
    """
    with _lock:
        data = _load()
        connection = next((c for c in data["connections"] if c.get("id") == connection_id), None)
        if connection is None:
            return
        ref = connection.get("secretRef")
        if ref and not keep_secret and ref not in LEGACY_SECRET_REFS:
            delete_secret(ref)
        data["connections"] = [c for c in data["connections"] if c.get("id") != connection_id]
        _save(data)
