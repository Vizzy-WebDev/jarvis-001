"""The model registry: what models exist, and what is true about each one.

A "model" is one model name under a connection. `data/models.json` stores only
`connectionId` plus the model's own label/caps/tier — the connection's adapter,
address, secret and key policy are hydrated in at READ time, so a model can never
drift out of sync with the connection it belongs to.

That read-time hydration is also how every capability flag added since a model
was saved simply appears, with no data migration and no rewrite of the user's
file.

**Availability is deliberately NOT stored here.** It lives in its own file
(`availability.py`) because this one is low-frequency, user-owned config and that
one is high-frequency machine-written state — and because writing both through
the same read-whole-file/modify/write-whole-file path is what silently lost
results when three health checks ran at once.
"""

from __future__ import annotations

import re
import threading
from typing import Any

from ..config import get_secret
from ..store import read_json, write_json
from .catalog import catalog_defaults, infer_billing, with_capability_defaults
from .connections import get_connection, list_connections
from .providers import provider_for_legacy

FILE = "models"

#: What may be changed through `update_model`. Adapter/baseUrl/secretRef/
#: connectionId are absent on purpose: setting any of them here would desync a
#: model from its connection, which is exactly what hydration exists to prevent.
PATCH_KEYS = ("label", "model", "enabled", "caps", "tier", "tags", "notes", "billing")

_lock = threading.RLock()


def _load() -> dict[str, Any]:
    data = read_json(FILE, {"entries": []})
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        return {"entries": []}
    return data


def _save(data: dict[str, Any]) -> None:
    write_json(FILE, data)


def _connections_by_id() -> dict[str, dict[str, Any]]:
    return {c["id"]: c for c in list_connections() if c.get("id")}


def hydrate(entry: dict[str, Any], by_id: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Merge in the owning connection's facts. Falls back to any inline copy on
    the entry itself, so a half-finished migration is harmless rather than
    producing a model that cannot be called."""
    by_id = _connections_by_id() if by_id is None else by_id
    conn = by_id.get(entry.get("connectionId") or "") or {}
    adapter = conn.get("adapter") or entry.get("adapter")
    base_url = conn.get("baseUrl") or entry.get("baseUrl")
    kind = conn.get("kind")
    if kind is None and conn:
        kind = provider_for_legacy(conn.get("adapter"), conn.get("baseUrl"))["kind"]

    hydrated = dict(entry)
    hydrated.update({
        "adapter": adapter,
        "baseUrl": base_url,
        "secretRef": conn.get("secretRef") or entry.get("secretRef"),
        "keyRequired": conn.get("keyRequired"),
        "kind": kind,
        "provider": conn.get("provider"),
        "connectionLabel": conn.get("label"),
        "caps": with_capability_defaults(entry.get("caps"), adapter, entry.get("model", ""), base_url, kind),
    })
    return hydrated


def list_models() -> list[dict[str, Any]]:
    by_id = _connections_by_id()
    return [hydrate(e, by_id) for e in _load()["entries"]]


def get_model(model_id: str) -> dict[str, Any] | None:
    entry = next((e for e in _load()["entries"] if e.get("id") == model_id), None)
    return hydrate(entry) if entry else None


def _make_id(seed: str, existing: list[dict[str, Any]]) -> str:
    base = re.sub(r"(^-|-$)", "", re.sub(r"[^a-z0-9]+", "-", str(seed or "model").lower().strip())) or "model"
    taken = {e.get("id") for e in existing}
    candidate, n = base, 2
    while candidate in taken:
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def add_model(
    *, connection_id: str, model: str, label: str | None = None,
    caps: dict[str, Any] | None = None, tier: dict[str, Any] | None = None,
    tags: list[str] | None = None, notes: str = "", enabled: bool = True,
    billing: str | None = None,
) -> dict[str, Any]:
    if not connection_id or not model:
        raise ValueError("A model needs a connection and a model name.")
    conn = get_connection(connection_id)
    if conn is None:
        raise KeyError(f"Unknown connection: {connection_id}")

    with _lock:
        data = _load()
        # Uniqueness is (connection, model), never model alone — the same model
        # name legitimately exists twice under two different keys.
        if any(e.get("connectionId") == connection_id and e.get("model") == model
               for e in data["entries"]):
            raise ValueError(f'"{model}" is already added under this connection.')

        kind = conn.get("kind") or provider_for_legacy(conn.get("adapter"), conn.get("baseUrl"))["kind"]
        defaults = catalog_defaults(conn.get("adapter"), model, conn.get("baseUrl"), kind)
        entry = {
            "id": _make_id(label or model, data["entries"]),
            "label": label or model,
            "model": model,
            "connectionId": connection_id,
            "enabled": enabled,
            "caps": {**defaults["caps"], **(caps or {})},
            "tier": {**defaults["tier"], **(tier or {})},
            "tags": tags if tags else defaults["tags"],
            "notes": notes or "",
            "billing": billing if billing is not None
            else infer_billing(conn.get("adapter"), conn.get("baseUrl"), model, kind),
        }
        data["entries"].append(entry)
        _save(data)
        return hydrate(entry)


def add_models(connection_id: str, models: list[Any]) -> dict[str, list[Any]]:
    added, failed = [], []
    for item in models or []:
        spec = {"model": item} if isinstance(item, str) else dict(item)
        try:
            caps = ({"contextTokens": spec["contextTokens"]}
                    if spec.get("contextTokens") is not None else None)
            added.append(add_model(connection_id=connection_id, model=spec["model"],
                                   label=spec.get("label"), billing=spec.get("billing"), caps=caps))
        except Exception as err:  # noqa: BLE001
            failed.append({"model": spec.get("model"), "error": str(err) or "Could not add this model."})
    return {"added": added, "failed": failed}


def update_model(model_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        data = _load()
        index = next((i for i, e in enumerate(data["entries"]) if e.get("id") == model_id), -1)
        if index == -1:
            raise KeyError(f"Unknown model: {model_id}")
        entry = dict(data["entries"][index])
        for key in PATCH_KEYS:
            if key in patch:
                entry[key] = patch[key]
        data["entries"][index] = entry
        _save(data)
        return hydrate(entry)


def delete_model(model_id: str) -> None:
    with _lock:
        data = _load()
        data["entries"] = [e for e in data["entries"] if e.get("id") != model_id]
        _save(data)


def delete_connection(connection_id: str) -> int:
    """Remove a connection and every model under it. Returns how many models went.

    Cascading is the caller-facing behaviour; `connections.remove_connection`
    deliberately does not do it, so a caller has to choose.
    """
    from . import connections as connection_store

    with _lock:
        data = _load()
        doomed = [e for e in data["entries"] if e.get("connectionId") == connection_id]
        data["entries"] = [e for e in data["entries"] if e.get("connectionId") != connection_id]
        _save(data)
    connection_store.remove_connection(connection_id)
    return len(doomed)


def is_ready(entry: dict[str, Any]) -> bool:
    """Whether this model has whatever credential it actually needs.

    The connection's stored `keyRequired` fact decides when present. The host
    regex below is only the fallback for a connection saved before that fact
    existed.
    """
    key_required = entry.get("keyRequired")
    if isinstance(key_required, bool):
        if not key_required:
            return True
        ref = entry.get("secretRef")
        return bool(ref and get_secret(ref))
    if (entry.get("adapter") == "openai-compatible" and entry.get("baseUrl")
            and not re.search(r"openai\.com", entry["baseUrl"], re.I)):
        return True
    ref = entry.get("secretRef")
    return bool(ref and get_secret(ref))
