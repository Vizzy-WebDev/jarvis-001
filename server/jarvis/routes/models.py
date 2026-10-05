"""Provider connections and the models listed under them, over HTTP — read from and
written to the model layer's config file (`models.yaml`).

The same shapes the Model Settings screen has always read. Behind them:

* **Test** and **Refresh models** both ask the connection what it serves
  (discovery). What that says is the connection's status; a failure keeps the last
  listing and never blocks adding a model by hand.
* A connection is added from a preset (data, `models/data/defaults.yaml`), with its
  key saved to `.env` and only the key's name in config.
* The person's chosen model is the alias `selected`; Auto is no alias.

A key is accepted here and never sent back: responses say only whether one is set.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from .. import config as secrets
from ..models import config, discovery, engine, settings, state
from ..models.catalog import endpoint_id

router = APIRouter(prefix="/api/models")


def _fail(message: str, status: int, **extra: Any) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message, **extra}, status_code=status)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _address(value: str) -> str | None:
    """A web address as typed, checked but never rewritten."""
    return value.rstrip("/") if re.match(r"^https?://[^/\s]+", value) else None


def _preset(preset_id: str) -> Any:
    return next((p for p in config.current().presets if p.id == preset_id), None)


def _secret_ref(name: str) -> str:
    return "model_" + re.sub(r"[^a-z0-9]", "_", name)


# --- the picker's contents (registered before /{id} so 'kinds' is not read as one) ----

@router.get("/kinds")
def list_kinds() -> dict[str, Any]:
    cfg = config.current()
    return {"kinds": [{"id": p.id, "label": p.label, "blurb": p.blurb, "address": p.address,
                       "defaultAddress": p.base_url, "key": p.key, "chooseFormat": p.driver is None}
                      for p in cfg.presets],
            "formats": [{"id": d, "label": label} for d, label in cfg.driver_labels.items()]}


@router.get("")
def list_connections() -> Any:
    try:
        names = list(config.current().connections)
    except config.ConfigError as err:
        return _fail(str(err), 500, connections=[], **settings.selection_view())
    notices = settings.notices()
    return {"connections": [settings.connection_view(n) for n in names], **settings.selection_view(),
            **({"notices": notices} if notices else {})}


@router.post("/select")
def select(body: dict[str, Any] = Body(default_factory=dict)):
    if body.get("auto") is True:  # "let Jarvis choose" — the alternative to naming a model
        settings.select_auto()
        return {"ok": True, **settings.selection_view()}
    connection, model_id = _text(body.get("providerId")), _text(body.get("modelId"))
    if not connection or not model_id:
        return _fail("Choose a model to select.", 400)
    try:
        stored = settings.select(connection, model_id, _text(body.get("effort")) or None)
    except LookupError as err:
        return _fail(str(err), 404)
    return {"ok": True, "selection": {"auto": False, **stored},
            "availability": settings.selection_view()["availability"]}


# --- connections ------------------------------------------------------------------------

def _refresh(name: str) -> dict[str, Any]:
    before = {e.model_id for e in engine.catalog().endpoints.values() if e.connection == name}
    state.clear_refusal(name)  # the person is reconnecting it: a refusal from before no longer stands
    result = discovery.refresh_one(name)
    after = {e.model_id for e in engine.catalog().endpoints.values() if e.connection == name}
    return {**result, "added": len(after - before), "updated": len(after & before)}


@router.post("")
def add(body: dict[str, Any] = Body(default_factory=dict)):
    """Add a connection, then ask it what it serves. The connection is kept whatever
    the outcome: a local server that isn't running yet is still worth having, and
    its card says what is wrong."""
    preset = _preset(_text(body.get("kind")))
    if preset is None:
        return _fail("Choose which kind of provider this is.", 400)
    cfg = config.current()
    driver = preset.driver or _text(body.get("format"))
    if driver not in cfg.driver_labels:
        return _fail("Choose which type of API this provider uses.", 400)
    typed = _text(body.get("address"))
    if preset.address == "fixed" or (preset.address == "editable" and not typed):
        address = preset.base_url
    else:
        if not typed:
            return _fail("Enter the address of the provider, like https://example.com/v1.", 400)
        address = _address(typed)
        if address is None:
            return _fail("That doesn't look like a web address. It should start with http:// or https://.", 400)
    key = _text(body.get("apiKey"))
    if preset.key == "required" and not key:
        return _fail(f"{preset.label} needs an API key.", 400)

    label = _text(body.get("label")) or preset.label
    name = config.unique_name(label, set(cfg.connections))
    entry: dict[str, Any] = {"name": name, "label": label, "preset": preset.id, "driver": driver,
                             "base_url": address, "trust": preset.trust}
    if preset.quirks:
        entry["quirks"] = preset.quirks
    if preset.default_params:
        entry["default_params"] = dict(preset.default_params)
    if key:
        entry["secret_ref"] = _secret_ref(name)
        secrets.save_secret(entry["secret_ref"], key)
    try:
        config.add_connection(entry)
    except config.ConfigError as err:
        return _fail(str(err), 400)
    found = _refresh(name)
    return {"ok": True, "tested": {"ok": found["ok"] or bool(found.get("reachable")), "message": found["message"]},
            "discovery": {"ok": True, "added": found["added"], "updated": 0} if found["ok"] else None,
            "connection": settings.connection_view(name)}


@router.patch("/{connection_id}")
def edit(connection_id: str, body: dict[str, Any] = Body(default_factory=dict)):
    conn = config.current().connections.get(connection_id)
    if conn is None:
        return _fail("That connection doesn't exist.", 404)
    changes: dict[str, Any] = {}
    if "label" in body:
        label = _text(body.get("label"))
        if not label:
            return _fail("A connection needs a name.", 400)
        changes["label"] = label
    if "address" in body:
        preset = _preset(conn.preset or "")
        if preset is not None and preset.address == "fixed":
            return _fail("This provider's address can't be changed.", 400)
        address = _address(_text(body.get("address")))
        if address is None:
            return _fail("That doesn't look like a web address. It should start with http:// or https://.", 400)
        changes["base_url"] = address
    key = _text(body.get("apiKey"))
    if key:
        ref = conn.secret_ref or _secret_ref(connection_id)
        secrets.save_secret(ref, key)
        changes["secret_ref"] = ref
    try:
        if changes:
            config.update_connection(connection_id, **changes)
    except config.ConfigError as err:
        return _fail(str(err), 400)
    if key or "base_url" in changes:
        state.mark_untested(connection_id)  # what was last checked no longer describes it
    return {"ok": True, "connection": settings.connection_view(connection_id)}


@router.post("/{connection_id}/test")
def test(connection_id: str):
    if connection_id not in config.current().connections:
        return _fail("That connection doesn't exist.", 404)
    found = _refresh(connection_id)
    return {"ok": found["ok"] or bool(found.get("reachable")), "message": found["message"],
            "connection": settings.connection_view(connection_id)}


@router.post("/{connection_id}/discover")
def discover(connection_id: str):
    if connection_id not in config.current().connections:
        return _fail("That connection doesn't exist.", 404)
    found = _refresh(connection_id)
    if not found["ok"]:
        # Not-offered and failed are different answers; neither blocks adding a model by hand.
        unsupported = bool(found.get("unsupported"))
        return _fail(found["message"], 501 if unsupported else 502, unsupported=unsupported,
                     connection=settings.connection_view(connection_id))
    return {"ok": True, "added": found["added"], "updated": found["updated"],
            "connection": settings.connection_view(connection_id)}


@router.post("/{connection_id}/models")
def add_model(connection_id: str, body: dict[str, Any] = Body(default_factory=dict)):
    """A model named by hand. Needs nothing from discovery — that is the point of it."""
    cfg = config.current()
    if connection_id not in cfg.connections:
        return _fail("That connection doesn't exist.", 404)
    model_id = _text(body.get("modelId"))
    if not model_id:
        return _fail("Enter the model's ID, exactly as the provider names it.", 400)
    label = _text(body.get("label"))
    if model_id not in cfg.connections[connection_id].models:
        config.set_model(connection_id, model_id, {"label": label} if label else {})
    return {"ok": True, "connection": settings.connection_view(connection_id)}


@router.delete("/{connection_id}/models/{model_id:path}")
def remove_model(connection_id: str, model_id: str):
    """Takes a row off the list — a mistyped ID, or clutter. It is not an off switch:
    a model the provider still lists comes straight back the next time it is asked."""
    cfg = config.current()
    if connection_id not in cfg.connections:
        return _fail("That connection doesn't exist.", 404)
    if endpoint_id(connection_id, model_id) not in engine.catalog(cfg).endpoints:
        return _fail("That model isn't on the list.", 404)
    if model_id in cfg.connections[connection_id].models:
        config.remove_model(connection_id, model_id)
    state.forget_discovered_model(connection_id, model_id)
    return {"ok": True, "connection": settings.connection_view(connection_id)}


@router.delete("/{connection_id}")
def delete(connection_id: str):
    conn = config.current().connections.get(connection_id)
    if conn is None:
        return _fail("That connection doesn't exist.", 404)
    # A selection pointing at it is left standing, and reported as exactly that.
    config.remove_connection(connection_id)
    state.forget_connection(connection_id)
    if conn.secret_ref:
        secrets.delete_secret(conn.secret_ref)
    return {"ok": True, **settings.selection_view()}
