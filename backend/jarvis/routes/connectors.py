"""Connectors over HTTP: what is connected, and what each one may do.

Two different questions live here and stay separate. Whether a connector is on
at all is one setting; whether Jarvis may use a particular tool of it is another,
per tool, and neither implies the other.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..connectors import capabilities as connector_capabilities
from ..connectors import store

router = APIRouter(prefix="/api/connectors")


def _sync() -> None:
    from ..assembly import get_registry

    connector_capabilities.sync(get_registry())


def _public(connector: dict) -> dict:
    """Everything except the secret.

    `config` is replaced by a summary rather than filtered: an allowlist of what
    to strip is a list somebody forgets to add to, and a key leaving the server
    because a new config field was not on it is the ordinary way that happens.
    """
    config = connector.get("config") or {}
    return {
        "id": connector["id"], "type": connector["type"], "label": connector.get("label"),
        "description": connector.get("description"),
        "enabled": connector.get("enabled", True),
        "status": connector.get("status") or {"state": "untested", "checkedAt": None,
                                              "detail": None},
        "source": connector.get("source") or {"type": "user"},
        "createdAt": connector.get("createdAt"), "updatedAt": connector.get("updatedAt"),
        "config": {"hasSecret": bool(config.get("secretRef"))},
    }


@router.get("")
async def listed():
    # The files allowlist is Jarvis's own ability rather than something added, so
    # it exists as soon as anything asks — an empty allowlist and no row at all
    # mean the same thing, and the row is the one that can be shown.
    store.get_or_create_singleton("files")
    return {"connectors": [_public(c) for c in store.list_connectors()]}


@router.get("/{connector_id}")
async def detail(connector_id: str):
    connector = store.get_connector(connector_id)
    if connector is None:
        return JSONResponse({"ok": False, "error": "Unknown connector."}, status_code=404)
    tools = connector_capabilities.connector_specs(connector)
    return {"ok": True, "connector": _public(connector),
            "tools": [{"name": s.name, "description": s.description,
                       "confirms": s.risk.value != "low"} for s in tools]}


@router.post("")
async def create(body: dict):
    try:
        connector = store.add_connector(type=str(body.get("type") or ""),
                                        label=str(body.get("label") or ""),
                                        config=body.get("config") or {})
    except ValueError as err:
        return JSONResponse({"ok": False, "error": str(err)}, status_code=400)
    _sync()
    return {"ok": True, "connector": _public(connector)}


@router.patch("/{connector_id}")
async def update(connector_id: str, body: dict):
    if store.get_connector(connector_id) is None:
        return JSONResponse({"ok": False, "error": "Unknown connector."}, status_code=404)

    patch = {}
    if "enabled" in body:
        patch["enabled"] = bool(body["enabled"])
    if "label" in body:
        patch["label"] = str(body["label"])
    if patch:
        store.update_connector(connector_id, patch)

    if body.get("toolPermissions"):
        for tool, permission in body["toolPermissions"].items():
            try:
                store.set_tool_permission(connector_id, tool, str(permission))
            except (ValueError, KeyError) as err:
                return JSONResponse({"ok": False, "error": str(err)}, status_code=400)
    _sync()
    return {"ok": True, "connector": _public(store.get_connector(connector_id))}


@router.post("/{connector_id}/refresh")
async def refresh(connector_id: str):
    try:
        result = connector_capabilities.refresh_tools(connector_id)
    except KeyError:
        return JSONResponse({"ok": False, "error": "Unknown connector."}, status_code=404)
    except Exception as err:  # noqa: BLE001 — an unreachable server is a result, not a crash
        return JSONResponse({"ok": False, "error": str(err)}, status_code=502)
    _sync()
    return {"ok": True, "tools": result["tools"],
            "connector": _public(result["connector"])}


@router.delete("/{connector_id}")
async def remove(connector_id: str):
    if store.get_connector(connector_id) is None:
        return JSONResponse({"ok": False, "error": "Unknown connector."}, status_code=404)
    store.delete_connector(connector_id)
    _sync()
    return {"ok": True}
