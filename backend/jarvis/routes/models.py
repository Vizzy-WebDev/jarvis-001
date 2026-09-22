"""Provider connections and the models listed under them, over HTTP.

The connection is the thing a person manages. Three operations on it are kept
strictly separate, because each answers a different question:

* **Test** — can Jarvis reach it, and does it accept the key? The only thing that
  sets a connection's status.
* **Discover** — what does it offer? Never touches the status, and never stands in
  the way of adding a model by hand.
* **Run** — happens in a turn, and is reported there.

A key is accepted here and never sent back: responses say only whether one is set.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from .. import config
from ..models import kinds, providers, selection, store
from ..models.errors import ProviderError, Unsupported
from ..models.types import CheckResult

router = APIRouter(prefix="/api/models")


def _fail(message: str, status: int, **extra: Any) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message, **extra}, status_code=status)


def _model_view(model: store.Model, connection: store.Connection) -> dict[str, Any]:
    effort = (model.facts or {}).get("effort")
    # Listed unless a discovery that succeeded AFTER this model was last seen no longer named it.
    dropped = bool(model.source == "discovered" and connection.discovered_at and model.last_seen_at
                   and model.last_seen_at < connection.discovered_at)
    return {
        "id": model.model_id,
        "label": model.label or model.model_id,
        "source": model.source,
        "stillListed": not dropped,
        "effort": {"levels": effort.get("levels") or [], "default": effort.get("default")} if effort else None,
    }


def _view(connection: store.Connection) -> dict[str, Any]:
    """Everything about a connection except its key."""
    kind = kinds.KINDS.get(connection.kind)
    return {
        "id": connection.id,
        "kind": connection.kind,
        "kindLabel": kind.label if kind else connection.kind,
        "format": connection.format,
        "label": connection.label,
        "address": kinds.base_url_for(connection.kind, connection.base_url),
        "addressEditable": bool(kind and kind.address != "fixed"),
        "keyNeeded": kind.key if kind else "none",
        "hasKey": bool(connection.secret_ref and config.get_secret(connection.secret_ref)),
        "state": connection.state,
        "detail": connection.detail,
        "checkedAt": connection.checked_at,
        "discoveredAt": connection.discovered_at,
        "models": [_model_view(m, connection) for m in store.list_models(connection.id)],
    }


def _selection_view() -> dict[str, Any]:
    provider_id, model_id, effort = selection.chosen()
    state = selection.availability()
    return {
        "selection": {"auto": selection.is_auto(), "providerId": provider_id, "modelId": model_id,
                      "effort": effort},
        "availability": {"state": state.state, "message": state.message},
    }


def _run_check(connection: store.Connection) -> CheckResult:
    """Test a connection and record the outcome. The only place a status is set."""
    try:
        result = providers.for_format(connection.format).check(selection.target_for(connection))
    except ProviderError as err:
        result = CheckResult(False, str(err))
    store.record_check(connection.id, "ok" if result.ok else "error", result.message)
    return result


def _run_discovery(connection: store.Connection) -> dict[str, Any]:
    """Ask the provider what it offers. Reports; never changes the status."""
    try:
        found = providers.for_format(connection.format).discover(selection.target_for(connection))
    except Unsupported as err:
        return {"ok": False, "unsupported": True, "message": str(err)}
    except ProviderError as err:
        return {"ok": False, "unsupported": False, "message": str(err)}
    # Whether this connection used to look like a gateway (some of its models marked as
    # their own router) is worth knowing if a fresh discovery no longer sees that — a
    # note in THIS response, never in the connection's status: discovery doesn't own that
    # (see the module docstring). Auto itself degrades safely either way; this is only so
    # the person isn't left assuming routing is still happening when it silently stopped.
    had_routers = any((m.facts or {}).get("router") for m in store.list_models(connection.id))
    result = store.record_discovery(connection.id, found)
    has_routers = any((item.facts or {}).get("router") for item in found)
    note = ("This connection used to report models that route and fall back on their own; the "
            "latest check no longer sees any, so Jarvis will try its models individually again."
            ) if had_routers and not has_routers else None
    return {"ok": True, **result, **({"note": note} if note else {})}


def _learn_what_providers_say() -> None:
    """Auto reads what each provider has reported about its models (tool support, what
    it takes in and gives out, price). Models listed before that was recorded have none
    of it, so a connection whose models carry no facts is asked once more for its list —
    the same call as "Refresh models", best-effort, and never a change to any status."""
    for connection in store.list_connections():
        models = store.list_models(connection.id)
        if models and all(m.facts is None for m in models):
            _run_discovery(connection)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


# --- the picker's contents (registered before /{id} so 'kinds' is not read as one) ----

@router.get("/kinds")
def list_kinds() -> dict[str, Any]:
    return {"kinds": kinds.public_kinds(), "formats": kinds.public_formats()}


@router.get("")
def list_connections() -> dict[str, Any]:
    return {"connections": [_view(c) for c in store.list_connections()], **_selection_view()}


@router.post("/select")
def select(body: dict[str, Any] = Body(default_factory=dict)):
    if body.get("auto") is True:  # "let Jarvis choose" — the alternative to naming a model
        selection.set_auto()
        _learn_what_providers_say()
        return {"ok": True, **_selection_view()}
    provider_id, model_id = _text(body.get("providerId")), _text(body.get("modelId"))
    if not provider_id or not model_id:
        return _fail("Choose a model to select.", 400)
    try:
        stored = selection.set_selection(provider_id, model_id, _text(body.get("effort")) or None)
    except LookupError as err:
        return _fail(str(err), 404)
    return {"ok": True, "selection": {"auto": False, "providerId": stored["selectedProviderId"],
                                      "modelId": stored["selectedModelId"],
                                      "effort": stored["selectedEffort"]},
            "availability": _selection_view()["availability"]}


# --- connections ------------------------------------------------------------------------

@router.post("")
def add(body: dict[str, Any] = Body(default_factory=dict)):
    """Add a connection, then test it and — if that worked — look for its models.

    The connection is kept whatever the outcome: a local server that isn't running
    yet is still worth having, and its card says what is wrong.
    """
    kind = kinds.KINDS.get(_text(body.get("kind")))
    if kind is None:
        return _fail("Choose which kind of provider this is.", 400)
    format_id = kind.format or _text(body.get("format"))
    if format_id not in kinds.FORMATS:
        return _fail("Choose which type of API this provider uses.", 400)
    try:
        address = kinds.normalize_base_url(kind.id, _text(body.get("address")))
    except kinds.InvalidAddress as err:
        return _fail(str(err), 400)
    key = _text(body.get("apiKey"))
    if kind.key == "required" and not key:
        return _fail(f"{kind.label} needs an API key.", 400)

    connection_id = store.new_id()
    secret_ref = None
    if key:
        secret_ref = f"model_{connection_id}"
        config.save_secret(secret_ref, key)
    connection = store.add_connection(
        connection_id=connection_id, kind=kind.id, format=format_id,
        label=_text(body.get("label")) or kind.label, base_url=address, secret_ref=secret_ref)

    tested = _run_check(connection)
    discovery = _run_discovery(connection) if tested.ok else None
    return {"ok": True, "tested": {"ok": tested.ok, "message": tested.message}, "discovery": discovery,
            "connection": _view(store.get_connection(connection_id))}


@router.patch("/{connection_id}")
def edit(connection_id: str, body: dict[str, Any] = Body(default_factory=dict)):
    connection = store.get_connection(connection_id)
    if connection is None:
        return _fail("That connection doesn't exist.", 404)
    changes: dict[str, Any] = {}
    if "label" in body:
        label = _text(body.get("label"))
        if not label:
            return _fail("A connection needs a name.", 400)
        changes["label"] = label
    if "address" in body:
        try:
            changes["base_url"] = kinds.normalize_base_url(connection.kind, _text(body.get("address")))
        except kinds.InvalidAddress as err:
            return _fail(str(err), 400)
    key = _text(body.get("apiKey"))
    if key:
        ref = connection.secret_ref or f"model_{connection.id}"
        config.save_secret(ref, key)
        changes["secret_ref"] = ref
    updated = store.update_connection(connection_id, **changes) if changes else connection
    if key or "base_url" in changes:
        # What was last tested no longer describes this connection.
        store.record_check(connection_id, "untested", None)
        updated = store.get_connection(connection_id)
    return {"ok": True, "connection": _view(updated)}


@router.post("/{connection_id}/test")
def test(connection_id: str):
    connection = store.get_connection(connection_id)
    if connection is None:
        return _fail("That connection doesn't exist.", 404)
    result = _run_check(connection)
    return {"ok": result.ok, "message": result.message, "connection": _view(store.get_connection(connection_id))}


@router.post("/{connection_id}/discover")
def discover(connection_id: str):
    connection = store.get_connection(connection_id)
    if connection is None:
        return _fail("That connection doesn't exist.", 404)
    result = _run_discovery(connection)
    if not result["ok"]:
        # Not-offered and failed are different answers, and neither blocks adding a model by hand.
        return _fail(result["message"], 501 if result["unsupported"] else 502,
                     unsupported=result["unsupported"], connection=_view(connection))
    response: dict[str, Any] = {"ok": True, "added": result["added"], "updated": result["updated"],
                                "connection": _view(store.get_connection(connection_id))}
    if result.get("note"):
        response["note"] = result["note"]
    return response


@router.post("/{connection_id}/models")
def add_model(connection_id: str, body: dict[str, Any] = Body(default_factory=dict)):
    """A model named by hand. Needs nothing from discovery — that is the point of it."""
    connection = store.get_connection(connection_id)
    if connection is None:
        return _fail("That connection doesn't exist.", 404)
    model_id = _text(body.get("modelId"))
    if not model_id:
        return _fail("Enter the model's ID, exactly as the provider names it.", 400)
    store.add_manual_model(connection_id, model_id, _text(body.get("label")) or None)
    return {"ok": True, "connection": _view(store.get_connection(connection_id))}


@router.delete("/{connection_id}/models/{model_id:path}")
def remove_model(connection_id: str, model_id: str):
    """Takes a row off the list — a mistyped ID, or clutter. It is not an off switch:
    a model the provider still lists comes straight back the next time it is asked."""
    if store.get_connection(connection_id) is None:
        return _fail("That connection doesn't exist.", 404)
    if not store.remove_model(connection_id, model_id):
        return _fail("That model isn't on the list.", 404)
    return {"ok": True, "connection": _view(store.get_connection(connection_id))}


@router.delete("/{connection_id}")
def delete(connection_id: str):
    connection = store.get_connection(connection_id)
    if connection is None:
        return _fail("That connection doesn't exist.", 404)
    store.delete_connection(connection_id)
    if connection.secret_ref:
        config.delete_secret(connection.secret_ref)
    return {"ok": True, **_selection_view()}
