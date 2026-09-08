"""Models and connections.

A secret is never in a response, on any route here. `secretRef` is stripped and
replaced by `hasSecret`: a screen needs to know whether a key is set, never what
it is. A secret goes IN (adding a connection, changing a key) and never comes
back out.

The logic behind adding, probing and discovering lives in `gateway/setup.py`, so
it can be exercised without HTTP; these routes are the surface over it.

**Rechecking every model is rate-limited on purpose.** The original fired every
enabled model's test simultaneously and was confirmed live to have mass-banned a
real roster — ten Gemini models all stamped unreachable inside one 150ms window,
seven of them working the moment each was retried alone — and to burn half a
day of a free tier in one click. Three at a time, and a preview route that says
what a full check will cost before anyone presses it.
"""

from __future__ import annotations

from typing import Any

import logging
import threading

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from ..gateway import availability, connections, providers, registry, setup
from ..gateway.error_kind import availability_state_for
from ..gateway.probe import probe_endpoint

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/models")
#: Connections are their own noun, and the original serves them under their own
#: path even though they are read back through /api/models.
connections_router = APIRouter(prefix="/api/connections")

#: How many model tests may be in flight at once. See this module's own note.
RECHECK_CONCURRENCY = 3


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


# --- adding, changing, removing --------------------------------------------

@connections_router.post("")
def add(body: dict[str, Any] = Body(default_factory=dict)):
    if not body.get("provider") and not body.get("adapter"):
        return JSONResponse({"ok": False, "error": "Please choose a provider."}, status_code=400)
    try:
        result = setup.create_connection_with_models(
            provider=body.get("provider"), adapter=body.get("adapter"),
            base_url=body.get("baseUrl"), label=body.get("label"), secret=body.get("secret"),
            models=body.get("models"), resolved=body.get("resolved"))
    except (ValueError, KeyError) as err:
        return JSONResponse({"ok": False, "error": str(err) or "Could not add that connection."},
                            status_code=400)
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)

    models = registry.list_models()
    return {"ok": True, "connection": _public_connection(result["connection"], models),
            "added": [_public_model(m) for m in result["added"]],
            "failed": result["failed"], "steps": result.get("steps")}


@connections_router.post("/probe")
def probe(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    """Try an address whose wire format is not known yet.

    Answers with every attempt it made, in plain language, so a failure can be
    explained rather than reduced to one generic sentence — the whole reason
    this exists. A 401 with no key supplied is "reached it, needs a key", never
    "that key is invalid".
    """
    result = probe_endpoint(str(body.get("baseUrl") or ""), body.get("secret"))
    return {"ok": result.ok, "steps": result.steps, "adapter": result.adapter,
            "baseUrl": result.base_url, "kind": result.kind,
            "keyRequired": result.key_required, "models": result.models,
            "error": result.error, "needsKey": result.needs_key}


@connections_router.post("/discover")
def discover(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return setup.discover_models(
        adapter=body.get("adapter") or "openai-compatible", base_url=body.get("baseUrl"),
        secret=body.get("secret"), connection_id=body.get("connectionId"))


@connections_router.patch("/{connection_id}")
def edit_connection(connection_id: str, body: dict[str, Any] = Body(default_factory=dict)):
    patch = {k: body[k] for k in ("label", "baseUrl", "secret") if k in body}
    try:
        conn = connections.update_connection(connection_id, patch)
    except KeyError:
        return JSONResponse({"ok": False, "error": "Unknown connection."}, status_code=404)
    except ValueError as err:
        return JSONResponse({"ok": False, "error": str(err)}, status_code=400)
    return {"ok": True, "connection": _public_connection(conn, registry.list_models())}


@connections_router.delete("/{connection_id}")
def remove_connection(connection_id: str) -> dict[str, Any]:
    """Removing a connection removes the models that hung off it — they cannot
    answer without it. The count is reported so the screen can say so."""
    removed = registry.delete_connection(connection_id)
    connections.remove_connection(connection_id)
    return {"ok": True, "removedModels": removed}


@router.post("")
def add_models(body: dict[str, Any] = Body(default_factory=dict)):
    """More models under a connection that already works.

    No test: the address and key were validated when the connection was added,
    and re-testing here would mean one live API call per model.
    """
    connection_id = body.get("connectionId")
    models = body.get("models")
    if not connection_id or not isinstance(models, list) or not models:
        return JSONResponse({"ok": False, "error": "Pick a connection and at least one model."},
                            status_code=400)
    try:
        result = registry.add_models(connection_id, models)
    except KeyError:
        return JSONResponse({"ok": False, "error": "Unknown connection."}, status_code=404)
    return {"ok": True, "added": [_public_model(m) for m in result["added"]],
            "failed": result["failed"]}


@router.patch("/{model_id}")
def edit_model(model_id: str, body: dict[str, Any] = Body(default_factory=dict)):
    try:
        entry = registry.update_model(model_id, body or {})
    except KeyError:
        return JSONResponse({"ok": False, "error": "Unknown model."}, status_code=404)
    return {"ok": True, "model": _public_model(entry)}


@router.delete("/{model_id}")
def remove_model(model_id: str) -> dict[str, Any]:
    registry.delete_model(model_id)
    return {"ok": True}


# --- is it actually working ------------------------------------------------

def _test_and_record(entry: dict[str, Any]) -> dict[str, Any]:
    """Test one model and let the result update its availability badge.

    Bookkeeping must never turn a working test into a failed request, so a
    failure to RECORD is logged and swallowed — the test's own answer stands.
    """
    from ..adapters import get_adapter

    state = "unreachable"
    try:
        result = get_adapter(entry.get("adapter")).test_connection(entry)
    except Exception as err:  # noqa: BLE001 — an unreachable model is a result
        # The RAW error classifies more accurately than the sentence an adapter
        # already reduced it to: rate-limited and misconfigured are different
        # states with different retry behaviour.
        state = availability_state_for(err)
        result = {"ok": False, "error": str(err) or "That model could not be reached."}

    try:
        if result.get("ok"):
            availability.clear(entry["id"])
        else:
            availability.record(entry["id"], state, detail=result.get("error"))
    except Exception:  # noqa: BLE001
        logger.exception("could not record availability for %s", entry.get("id"))
    return result


@router.post("/{model_id}/test")
def test_model(model_id: str):
    entry = registry.get_model(model_id)
    if entry is None:
        return JSONResponse({"ok": False, "error": "Unknown model."}, status_code=404)
    return _test_and_record(entry)


@router.get("/recheck/preview")
def recheck_preview() -> dict[str, Any]:
    """What checking everything would cost, before anyone presses it.

    Read-only — it makes no model calls. It only reports what each provider has
    already told us about the credit left, where a provider reports that at all.
    """
    from ..cost import store as cost_store

    enabled = [e for e in registry.list_models() if e.get("enabled")]
    not_working = [e for e in enabled if not availability.is_eligible(e["id"])]

    by_connection = []
    for conn in connections.list_connections():
        count = sum(1 for e in enabled if e.get("connectionId") == conn.get("id"))
        if not count:
            continue
        balance = cost_store.get_balance(str(conn.get("provider") or "")) or {}
        by_connection.append({"id": conn.get("id"), "label": conn.get("label"), "count": count,
                              "isFreeTier": balance.get("isFreeTier"),
                              "remaining": balance.get("remaining")})
    return {"total": len(enabled), "notWorking": len(not_working), "byConnection": by_connection}


@router.post("/recheck")
def recheck(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    """`scope: "all"` checks every enabled model; anything else checks only the
    ones not currently working — the cheap default, since a model already
    answering needs no proof."""
    enabled = [e for e in registry.list_models() if e.get("enabled")]
    entries = (enabled if body.get("scope") == "all"
               else [e for e in enabled if not availability.is_eligible(e["id"])])

    # A simple pull-based pool rather than a library: this is the only place in
    # the app that needs one, and firing them all at once is what banned a real
    # roster (see this module's own note).
    queue = list(entries)
    lock = threading.Lock()

    def lane() -> None:
        while True:
            with lock:
                if not queue:
                    return
                entry = queue.pop(0)
            _test_and_record(entry)

    lanes = [threading.Thread(target=lane, daemon=True)
             for _ in range(min(RECHECK_CONCURRENCY, len(entries)))]
    for thread in lanes:
        thread.start()
    for thread in lanes:
        thread.join()

    return {"ok": True, "models": [_public_model(m) for m in registry.list_models()]}
