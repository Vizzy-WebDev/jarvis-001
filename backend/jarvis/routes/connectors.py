"""Connectors over HTTP: what is connected, and what each one may do.

Two different questions live here and stay separate. Whether a connector is on
at all is one setting; whether Jarvis may use a particular tool of it is another,
per tool, and neither implies the other.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..connectors import capabilities as connector_capabilities
from ..connectors import catalog, catalog_credentials, icons, oauth, store
from ..jscompat import now_iso

logger = logging.getLogger(__name__)

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
    store.get_or_create_singleton("browser")
    return {"connectors": [_public(c) for c in store.list_connectors()]}


def _connector_for_catalog_entry(catalog_entry_id: str) -> dict | None:
    """The connector record already backing a given catalog entry, if the
    user has ever clicked into it before — `None` for a never-touched entry."""
    for connector in store.list_connectors(kind="mcp"):
        source = connector.get("source") or {}
        if source.get("type") == "catalog" and source.get("id") == catalog_entry_id:
            return connector
    return None


# The Official Connectors directory — every entry gets one uniform Connect
# button in the UI, no "ready"/"needs setup" label. `connectorId`/`status` are
# only filled in once the user has actually clicked into that entry at least
# once. Registered ahead of GET /{connector_id} below so "catalog" is never
# swallowed as a connector id.
@router.get("/catalog")
async def catalog_listed():
    entries = []
    for entry in catalog.list_catalog():
        existing = _connector_for_catalog_entry(entry["id"])
        flow = entry.get("connectFlow") or {}
        entries.append({
            "id": entry["id"], "label": entry.get("label"), "icon": entry.get("icon"),
            "description": entry.get("description"),
            "connectFlow": {"kind": flow.get("kind"), "guide": flow.get("guide") or None},
            "connectorId": existing["id"] if existing else None,
            "status": (existing.get("status") or {}).get("state") if existing else None,
            # Cache-only read (refresh=False): resolving a logo makes a real
            # network call, which a listing response must never block on —
            # matches Node's own design, where GET .../catalog reads a cache
            # populated at startup rather than fetching per request. Present
            # whether or not this entry has ever been connected; null only
            # until the first resolve succeeds (icons.py's own auto-refresh
            # then keeps it current with no release needed).
            "iconDataUri": icons.icon_for(flow.get("url") or "", curated_key=entry["id"],
                                          refresh=False),
        })
    return {"catalog": entries}


@router.post("/catalog/{catalog_id}/ensure")
async def catalog_ensure(catalog_id: str):
    """Creates the underlying `mcp` connector record for a catalog entry on
    first click, or finds the existing one on any later one. Does NOT start
    OAuth itself — the caller decides that separately via POST
    /{connector_id}/connect once it knows whether to show a plain Connect
    button or a guided-setup form."""
    entry = catalog.get_entry(catalog_id)
    if entry is None:
        return JSONResponse({"ok": False, "error": "Jarvis doesn't have that connector yet."},
                            status_code=404)
    connector = _connector_for_catalog_entry(entry["id"])
    if connector is None:
        connect_flow = dict(entry.get("connectFlow") or {})
        registered = catalog_credentials.get_catalog_client(entry["id"])
        if registered:
            connect_flow["clientId"] = registered["clientId"]
        connector = store.add_connector(
            type="mcp", label=entry.get("label") or entry["id"],
            config={"connectFlow": connect_flow})
        store.update_connector(connector["id"], {
            "description": entry.get("description"),
            "source": {"type": "catalog", "id": entry["id"]}})
        if registered:
            from ..config import save_secret

            secret = catalog_credentials.get_catalog_client_secret(entry["id"])
            if secret:
                save_secret(f"connclient_{connector['id']}", secret)
    return {"ok": True, "connectorId": connector["id"]}


@router.post("/catalog/{catalog_id}/register-client")
async def catalog_register_client(catalog_id: str, body: dict):
    """Registers (or replaces) the Client ID/Secret shared by every connector
    this catalog entry ever creates — the one-time equivalent of a centrally-
    registered app."""
    entry = catalog.get_entry(catalog_id)
    if entry is None:
        return JSONResponse({"ok": False, "error": "Jarvis doesn't have that connector yet."},
                            status_code=404)
    client_id = (body or {}).get("clientId")
    client_secret = (body or {}).get("clientSecret")
    if not client_id:
        return JSONResponse({"ok": False, "error": "A Client ID is required."}, status_code=400)
    catalog_credentials.save_catalog_client(entry["id"], client_id, client_secret)
    return {"ok": True}


@router.delete("/catalog/{catalog_id}/register-client")
async def catalog_deregister_client(catalog_id: str):
    entry = catalog.get_entry(catalog_id)
    if entry is None:
        return JSONResponse({"ok": False, "error": "Jarvis doesn't have that connector yet."},
                            status_code=404)
    catalog_credentials.clear_catalog_client(entry["id"])
    return {"ok": True}


# The exact redirect address the server will actually send on every OAuth
# call — the guided-setup form displays THIS rather than computing its own
# from window.location, which can silently disagree with what the server
# actually uses. Registered ahead of GET /{connector_id} for the same reason
# as /catalog above.
@router.get("/oauth/redirect-uri")
async def oauth_redirect_uri():
    return {"uri": oauth.redirect_uri()}


@router.get("/oauth/callback")
async def oauth_callback(request: Request):
    outcome = await oauth.handle_callback(dict(request.query_params))
    if not outcome.get("ok") and outcome.get("connectorId"):
        try:
            store.update_connector(outcome["connectorId"], {
                "status": {"state": "error", "checkedAt": now_iso(), "detail": outcome.get("error")}})
        except Exception:  # noqa: BLE001 — best-effort; the page below still explains it
            logger.warning("could not record connector error status after OAuth callback")
    from .. import notifications

    try:
        notifications.add(
            kind="connector", level="success" if outcome.get("ok") else "error",
            title="Connector connected." if outcome.get("ok") else "Connector could not connect.",
            body="" if outcome.get("ok") else (outcome.get("error") or "Unknown error."),
            action={"label": "App Control", "section": "app-control"},
            meta={"connectorId": outcome["connectorId"]} if outcome.get("connectorId") else None)
    except Exception:  # noqa: BLE001 — the HTML page below still tells the user what happened
        logger.warning("could not record the OAuth callback notification")
    message = "Connected." if outcome.get("ok") else f"Could not connect: {outcome.get('error')}"
    html = (f'<!doctype html><html><body style="font:16px system-ui;padding:2em;text-align:center">'
           f'<p>{message}</p><p>You can close this tab.</p>'
           f'<script>setTimeout(function(){{ window.close(); }}, 1200);</script>'
           f'</body></html>')
    return HTMLResponse(content=html)


@router.post("/{connector_id}/connect")
async def connect(connector_id: str, body: dict | None = None):
    """The one place any `mcp` connector's OAuth flow actually starts — used
    for an Official entry (after /ensure above), a fresh Custom Connector, and
    a Reconnect on one that's fallen into an error state alike."""
    connector = store.get_connector(connector_id)
    if connector is None or connector.get("type") != "mcp":
        return JSONResponse({"ok": False, "error": "Unknown connector."}, status_code=404)
    server_url = ((connector.get("config") or {}).get("connectFlow") or {}).get("url")
    if not server_url:
        return JSONResponse({"ok": False, "error": "This connector has no server address to connect to."},
                            status_code=400)

    client_id = (body or {}).get("clientId")
    client_secret = (body or {}).get("clientSecret")
    if client_secret and not client_id:
        return JSONResponse({"ok": False, "error": "A Client Secret needs a Client ID to go with it."},
                            status_code=400)
    try:
        outcome = await oauth.start_connect(connector_id, server_url,
                                            manual_client_id=client_id or None,
                                            manual_client_secret=client_secret or None)
        return {"ok": True, "connectorId": connector_id, **outcome}
    except Exception as err:  # noqa: BLE001 — a real, hand-written oauth.py message goes straight to the user
        message = str(err) or "Could not start connecting that service."
        logger.warning("connector %s connect failed: %s", connector_id, message)
        try:
            store.update_connector(connector_id, {
                "status": {"state": "error", "checkedAt": now_iso(), "detail": message}})
        except Exception:  # noqa: BLE001 — best-effort
            pass
        return JSONResponse({"ok": False, "error": message}, status_code=400)


@router.post("/{connector_id}/disconnect")
async def disconnect(connector_id: str):
    """Signs a connected `mcp` connector out without deleting it — the record
    and its tool permissions survive, only the stored token is cleared.
    Distinct from DELETE (Remove): reconnecting after this needs no
    re-setup."""
    connector = store.get_connector(connector_id)
    if connector is None or connector.get("type") != "mcp":
        return JSONResponse({"ok": False, "error": "Unknown connector."}, status_code=404)
    oauth.disconnect(connector_id)
    config = dict(connector.get("config") or {})
    config.pop("secretRef", None)
    updated = store.update_connector(connector_id, {
        "config": {**config, "secretRef": None},
        "status": {"state": "untested", "checkedAt": now_iso(), "detail": None}})
    return {"ok": True, "connector": _public(updated)}


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

    # An API connector's key, saved server-side under a fresh ref immediately
    # — it never sits in config as plain JSON, and it's never echoed back
    # (_public() strips it down to hasSecret). Optional and separate from
    # `config` on purpose: a raw secret has no business riding inside a
    # generic config object a PATCH could otherwise echo back verbatim.
    api_key = body.get("apiKey")
    if api_key:
        from ..config import save_secret

        ref = f"conn_{connector['id']}"
        save_secret(ref, str(api_key))
        connector = store.update_connector(connector["id"], {"config": {"secretRef": ref}})

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
    # A generic config patch — e.g. an API connector's specUrl before a
    # refresh, or a CLI connector's reviewed command list. Merges one level
    # deep (store.py's update_connector), so this never has to resend a
    # stored secretRef or an already-cached tool list alongside it.
    if isinstance(body.get("config"), dict):
        patch["config"] = body["config"]
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
