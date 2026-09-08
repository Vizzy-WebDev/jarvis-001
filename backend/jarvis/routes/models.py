"""Models and connections, read only.

The half of the model surface a picker needs: what exists, what is ready, and
what is currently unreachable. Everything that WRITES — adding a connection,
probing an address, discovering what a server offers, entering a key — belongs
to the next wave and is deliberately absent here rather than half-built.

A secret is never in a response. `secretRef` is stripped and replaced by
`hasSecret`, exactly as the original does: a screen needs to know whether a key
is set, never what it is.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ..gateway import availability, connections, providers, registry

router = APIRouter(prefix="/api/models")


def _public_model(entry: dict[str, Any]) -> dict[str, Any]:
    rest = {k: v for k, v in entry.items() if k != "secretRef"}
    return {**rest, "hasSecret": bool(entry.get("secretRef")), "ready": registry.is_ready(entry)}


def _public_connection(connection: dict[str, Any], models: list[dict[str, Any]]) -> dict[str, Any]:
    """A connection saved before the provider catalogue existed has no
    provider/kind of its own; it is backfilled at READ time, never migrated —
    the same read-time pattern `registry.hydrate()` already uses."""
    rest = {k: v for k, v in connection.items() if k != "secretRef"}
    backfill = ({} if connection.get("provider")
                else providers.provider_for_legacy(connection.get("adapter"),
                                                   connection.get("baseUrl")))
    return {
        **rest, **backfill,
        "hasSecret": bool(connection.get("secretRef")),
        "modelCount": sum(1 for m in models if m.get("connectionId") == connection.get("id")),
    }


def _health(models: list[dict[str, Any]]) -> dict[str, Any]:
    """Which models are being skipped right now, and for how long.

    A flat map keyed by model id, as the original serves it. The fields come
    from this build's own availability record — `state` is what it calls the
    original's `kind`, and `detail` its `reason` — rather than inventing a
    second vocabulary for the same fact.
    """
    out: dict[str, Any] = {}
    for entry in models:
        model_id = entry.get("id")
        if not model_id or availability.is_eligible(model_id):
            continue
        record = availability.status_of(model_id) or {}
        out[model_id] = {
            "reason": record.get("detail"),
            "kind": record.get("state"),
            "retryInMs": availability.retry_after_ms(model_id),
        }
    return out


@router.get("")
def listed() -> dict[str, Any]:
    models = registry.list_models()
    return {
        "connections": [_public_connection(c, models) for c in connections.list_connections()],
        "models": [_public_model(m) for m in models],
        "health": _health(models),
    }


@router.get("/providers")
def provider_tiles() -> dict[str, Any]:
    """The five user-facing tiles the "add a model" flow shows.

    Data only, and deliberately not adapter names: which wire format a provider
    resolves to stays server-side. See `gateway/providers.py`.
    """
    return {"providers": providers.PROVIDERS}
