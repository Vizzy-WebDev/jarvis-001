"""The person's side of the model layer: what they selected, whether it can run,
and the views the Model Settings screen reads — all over the config file.

* The chosen model is the alias `selected`; Jarvis pins it. No `selected` alias
  means Auto: the task class's route decides.
* The chosen effort is a preference (`selectedEffort`) the boundary sends as a
  per-request hint — never part of the alias.
* A model the person chose is also written into config under its connection, so
  it stays whatever a later discovery lists.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .. import config as secrets
from .. import prefs
from . import capabilities as caps_mod
from . import config, engine, state
from .catalog import Endpoint, endpoint_id, split_endpoint_id
from .types import EFFORTS

SELECTED = "selected"


@dataclass(frozen=True)
class Availability:
    #: 'ok' | 'none' | 'missing_connection' | 'missing_model' | 'no_key' | 'config_error'
    state: str
    message: str | None


def is_auto(cfg: config.Config | None = None) -> bool:
    cfg = cfg or config.current()
    return SELECTED not in cfg.aliases


def chosen(cfg: config.Config | None = None) -> tuple[str | None, str | None]:
    """(connection, model id) of the selected alias, or (None, None) under Auto."""
    cfg = cfg or config.current()
    alias = cfg.aliases.get(SELECTED)
    if alias is None or not alias.endpoint:
        return None, None
    return split_endpoint_id(alias.endpoint)


def effort() -> str | None:
    value = prefs.get_prefs().get("selectedEffort")
    return value if value in EFFORTS else None


def effort_levels(endpoint: Endpoint | None) -> list[str]:
    """The canonical levels this endpoint accepts — only those, never assumed."""
    return list(caps_mod.effort_levels(endpoint.capabilities)) if endpoint else []


def _has_key(conn: Any) -> bool:
    return not conn.secret_ref or bool(secrets.get_secret(conn.secret_ref))


def availability() -> Availability:
    try:
        cfg = config.current()
    except config.ConfigError as err:
        return Availability("config_error", str(err))
    cat = engine.catalog(cfg)
    if is_auto(cfg):
        usable = [e for e in cat.endpoints.values() if _has_key(cat.connections[e.connection])]
        if usable:
            return Availability("ok", None)
        return Availability("none", "No model is connected yet. Connect a provider on the Model Settings screen.")
    conn_name, model_id = chosen(cfg)
    conn = cfg.connections.get(conn_name or "")
    if conn is None:
        return Availability("missing_connection",
                            f"The model you picked ({model_id}) came from a connection that has been removed. "
                            "Choose a model on the Model Settings screen.")
    if endpoint_id(conn.name, model_id or "") not in cat.endpoints:
        return Availability("missing_model", f"The model you picked ({model_id}) is no longer set up on "
                                             f"{conn.label or conn.name}. Choose a model on the Model Settings screen.")
    if not _has_key(conn):
        return Availability("no_key", f"{conn.label or conn.name} has no key saved. Add one on the Model Settings "
                                      "screen.")
    return Availability("ok", None)


def ready(limit: int = 5) -> list[Endpoint]:
    """What could answer right now — read from config and state; nothing is called."""
    if availability().state != "ok":
        return []
    cfg = config.current()
    cat = engine.catalog(cfg)
    if not is_auto(cfg):
        return cat.resolve_alias(SELECTED)
    return [e for e in cat.endpoints.values() if _has_key(cat.connections[e.connection])][:limit]


def select(connection: str, model_id: str, chosen_effort: str | None) -> dict[str, Any]:
    cfg = config.current()
    cat = engine.catalog(cfg)
    endpoint = cat.endpoints.get(endpoint_id(connection, model_id))
    if connection not in cfg.connections or endpoint is None:
        raise LookupError("That model isn't set up.")
    if chosen_effort not in effort_levels(endpoint):
        chosen_effort = None
    if not endpoint.configured:
        config.set_model(connection, model_id)
    config.set_alias(SELECTED, endpoint=endpoint.id)
    prefs.set_prefs({"selectedEffort": chosen_effort})
    return {"providerId": connection, "modelId": model_id, "effort": chosen_effort}


def select_auto() -> None:
    config.remove_alias(SELECTED)
    prefs.set_prefs({"selectedEffort": None})


def ensure_pin(pin: str | None) -> str | None:
    """A pin typed as a model id (the Specialists screen) becomes an alias of the same
    name, so callers only ever pin aliases. Found or refused, never approximated: a
    model on several connections goes to the selected one's, else it's ambiguous."""
    if not pin:
        return None
    cfg = config.current()
    if pin in cfg.aliases:
        return pin
    cat = engine.catalog(cfg)
    matches = [e for e in cat.endpoints.values() if e.model_id == pin]
    selected_conn, _ = chosen(cfg)
    preferred = [e for e in matches if e.connection == selected_conn]
    if preferred:
        matches = preferred
    if not matches:
        raise LookupError(f"The model “{pin}” isn't set up on any connection.")
    if len(matches) > 1:
        raise LookupError(f"The model “{pin}” is set up on more than one connection, so Jarvis can't tell which "
                          "you meant.")
    if not matches[0].configured:
        config.set_model(matches[0].connection, matches[0].model_id)
    config.set_alias(pin, endpoint=matches[0].id)
    return pin


def reported_prices() -> list[dict[str, Any]]:
    """Every price a connection's own model list reported at its last discovery, per token,
    filed under the provider name the cost ledger uses. Read from state; nothing is called."""
    out = []
    for conn in config.current().connections.values():
        for found in state.discovered(conn.name):
            if found.pricing is not None:
                out.append({"provider": conn.ledger_name, "model_id": found.model_id,
                            "price_in": found.pricing.input / 1_000_000,
                            "price_out": found.pricing.output / 1_000_000})
    return out


# --- the screen's views ----------------------------------------------------------------------

def connection_view(name: str) -> dict[str, Any]:
    cfg = config.current()
    conn = cfg.connections[name]
    preset = next((p for p in cfg.presets if p.id == conn.preset), None)
    status = state.discovery_status(name)
    ok = status.get("ok")
    endpoints = [e for e in engine.catalog(cfg).endpoints.values() if e.connection == name]
    discovered_before = bool(status.get("ok_at"))
    return {
        "id": conn.name,
        "kind": conn.preset or "custom",
        "kindLabel": preset.label if preset else (conn.preset or "Custom"),
        "format": conn.driver,
        "label": conn.label or conn.name,
        "address": conn.base_url,
        "addressEditable": not (preset and preset.address == "fixed"),
        "keyNeeded": preset.key if preset else ("optional" if conn.secret_ref else "none"),
        "hasKey": bool(conn.secret_ref and secrets.get_secret(conn.secret_ref)),
        "state": "untested" if ok is None else ("ok" if ok else "error"),
        "detail": status.get("error"),
        "checkedAt": status.get("at"),
        "discoveredAt": status.get("ok_at"),
        "models": [{
            "id": e.model_id,
            "label": e.label or e.model_id,
            "source": "manual" if e.configured and not e.listed else "discovered",
            # A model the person set up stays theirs whatever a listing says.
            "stillListed": e.listed or e.configured or not discovered_before,
            "effort": ({"levels": effort_levels(e), "default": caps_mod.effort_default(e.capabilities)}
                       if effort_levels(e) else None),
        } for e in endpoints],
    }


def selection_view() -> dict[str, Any]:
    try:
        cfg = config.current()
        conn_name, model_id = chosen(cfg)
        auto = is_auto(cfg)
    except config.ConfigError:
        conn_name, model_id, auto = None, None, False
    found = availability()
    return {"selection": {"auto": auto, "providerId": conn_name, "modelId": model_id,
                          "effort": effort() if not auto else None},
            "availability": {"state": found.state, "message": found.message}}


def notices() -> list[str]:
    return [n["text"] for n in state.notices()]
