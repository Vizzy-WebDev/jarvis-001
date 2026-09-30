"""The one-time move of the old model tables into the config and state files.

Run by migration 35 with the migration's own database connection (never
`get_db()`, which is still opening) — and again at every startup while the old
connections table still exists (`retry_if_pending`), which also covers a database
whose migration 35 was another branch's and so never ran this one. It exports, reads both files back and checks
every connection and model arrived, and only then lets the migration drop
`model_providers`, `provider_models` and `model_outcomes`. If anything goes wrong
the tables are left alone, the person is told in plain words, and the export is
tried again on the next start (`retry_if_pending`).

What moves where:

* each old connection -> a config connection (name from its label, driver from
  its format, its address, its `secret_ref` unchanged in `.env`; trust `local` for
  the two on-this-computer kinds, `standard` for everything else — nothing is ever
  guessed to be zero-retention);
* models added by hand, and the selected and pinned ones, -> config (so they stay
  whatever discovery later says); discovered models and what their provider
  reported about them -> the state file's last listing;
* `model_outcomes` -> state: latency and failures-in-a-row;
* the selected model -> alias `selected` (Auto -> no alias); its effort stays in
  preferences, where the boundary reads it per request;
* every model pin (a specialist's, a scheduled task's) -> an alias named exactly
  what the pin says, so the stored pins keep working unchanged. A pin that matches
  more than one connection is matched to one, and the person is told which.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

import yaml

from . import config as layer_config
from . import state
from .catalog import Pricing, endpoint_id
from .prepared import Discovered

logger = logging.getLogger(__name__)

TABLES = ("model_outcomes", "provider_models", "model_providers")
#: Where an old connection's models may be: this build's own table, or the same table
#: under the name another branch's migration renamed it to.
MODEL_TABLES = ("provider_models", "provider_catalog")

LEGACY_PATH = Path(__file__).resolve().parent / "data" / "legacy_import.yaml"


def _driver_for_format() -> dict[str, str]:
    return dict((yaml.safe_load(LEGACY_PATH.read_text(encoding="utf-8")) or {}).get("formats") or {})


def _presets() -> dict[str, Any]:
    """The old kinds are today's preset ids: their default addresses, trust and quirks."""
    return {p["id"]: p for p in layer_config.load_defaults().get("presets") or []}


class ExportFailed(RuntimeError):
    pass


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _tables_present(conn: sqlite3.Connection) -> bool:
    """Is there anything left to move? The connections table is what matters: the
    others may be missing, renamed or empty (a database that ran another branch's
    migrations has its models table under another name)."""
    return "model_providers" in _table_names(conn)


def _rows(conn: sqlite3.Connection, table: str, order: str) -> list[dict[str, Any]]:
    if table not in _table_names(conn):
        return []
    return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY {order}")]


def _facts(row: dict[str, Any]) -> dict[str, Any]:
    try:
        facts = json.loads(row["facts_json"]) if row.get("facts_json") else None
    except ValueError:
        facts = None
    return facts if isinstance(facts, dict) else {}


def _facts_to_capabilities(facts: dict[str, Any] | None) -> tuple[dict[str, Any], Pricing | None]:
    """Only what the provider reported, renamed into the capability vocabulary."""
    facts = facts or {}
    caps: dict[str, Any] = {}
    if isinstance(facts.get("maxOutput"), int) and facts["maxOutput"] > 0:
        caps["max_output_tokens"] = facts["maxOutput"]
    if (facts.get("effort") or {}).get("levels"):
        caps["reasoning_control"] = True
    if isinstance(facts.get("tools"), bool):
        caps["tools"] = facts["tools"]
    if isinstance(facts.get("image"), bool):
        caps["image_in"] = facts["image"]
    if facts.get("chat") is False:
        caps["text_in"] = False
    pricing = Pricing(0.0, 0.0) if facts.get("free") is True else None
    return caps, pricing


def _read_pins(conn: sqlite3.Connection) -> list[str]:
    """Every model pin stored anywhere: specialists' and scheduled tasks'."""
    pins: list[str] = []
    try:
        for row in conn.execute("SELECT DISTINCT model_pin FROM agents WHERE model_pin IS NOT NULL "
                                "AND model_pin != ''"):
            pins.append(str(row[0]))
    except sqlite3.OperationalError:
        pass  # no agents table in this database
    try:
        from ..scheduler import task_store

        for task in task_store.list_tasks():
            pin = ((task or {}).get("action") or {}).get("modelId")
            if pin:
                pins.append(str(pin))
    except Exception:  # noqa: BLE001 - tasks are a JSON file; a bad one must not stop the move
        logger.exception("couldn't read scheduled tasks' model pins")
    return list(dict.fromkeys(pins))


def _selection() -> tuple[bool, str | None, str | None]:
    from .. import prefs

    saved = prefs.get_prefs()
    return bool(saved.get("selectedAuto")), saved.get("selectedProviderId"), saved.get("selectedModelId")


def export(conn: sqlite3.Connection) -> list[str]:
    """Move everything; return the plain-language notes the person should see.
    Raises ExportFailed (tables untouched) if the result can't be verified."""
    conn_rows = _rows(conn, "model_providers", "created_at, id")
    model_table = next((t for t in MODEL_TABLES if t in _table_names(conn)), None)
    model_rows = _rows(conn, model_table, "provider_id, added_at") if model_table else []
    outcome_rows = _rows(conn, "model_outcomes", "provider_id")
    notes: list[str] = []

    drivers_for = _driver_for_format()
    presets = _presets()
    data = layer_config.raw()
    existing = {c.get("name") for c in data.get("connections") or [] if isinstance(c, dict)}
    taken = set(existing)
    name_for: dict[str, str] = {}  # every old connection -> the config connection that now serves it
    created: dict[str, str] = {}  # only the ones this move added
    entries: list[dict[str, Any]] = []
    for row in conn_rows:
        driver = drivers_for.get(row["format"])
        if driver is None:
            notes.append(f"Your connection “{row['label']}” used a format Jarvis no longer knows "
                         f"({row['format']}), so it wasn't moved.")
            continue
        preset = presets.get(row["kind"]) or {}
        base_url = row["base_url"] or preset.get("base_url") or ""
        already = next((c for c in data.get("connections") or [] if isinstance(c, dict)
                        and c.get("driver") == driver and c.get("base_url") == base_url), None)
        if already is not None:
            # The same service is already set up — moved by an earlier attempt, or added
            # again by the person meanwhile. Not duplicated; its key stays saved.
            name_for[row["id"]] = already["name"]
            if already.get("secret_ref") != (row["secret_ref"] or None):
                notes.append(f"Your earlier connection “{row['label']}” wasn't added again, because "
                             f"“{already.get('label') or already['name']}” already connects to the same address. "
                             "Its key is still saved, if you need it.")
            continue
        name = layer_config.unique_name(row["label"] or row["kind"], taken)
        taken.add(name)
        name_for[row["id"]] = name
        created[row["id"]] = name
        mine = [m for m in model_rows if m["provider_id"] == row["id"]]
        has_rich_listing = any(any(k in _facts(m) for k in ("chat", "tools", "image", "free", "router"))
                               for m in mine)
        local = preset.get("trust") == "local"
        entry: dict[str, Any] = {
            "name": name,
            "label": row["label"],
            "preset": row["kind"],
            "driver": driver,
            "base_url": base_url,
            "trust": "local" if local else "standard",
        }
        if row["secret_ref"]:
            entry["secret_ref"] = row["secret_ref"]
        quirks = preset.get("quirks") or ("gateway" if driver == "openai_chat" and has_rich_listing else None)
        if quirks:
            entry["quirks"] = quirks
        manual = {m["model_id"]: ({"label": m["label"]} if m["label"] else {})
                  for m in mine if m["source"] == "manual"}
        if manual:
            entry["models"] = manual
        entries.append(entry)
    data.setdefault("connections", [])
    data["connections"] = list(data["connections"] or []) + entries

    # Which endpoint each model id lives on, for the selection and the pins.
    model_ids_by_conn = {name_for[m["provider_id"]]: set() for m in model_rows if m["provider_id"] in name_for}
    for m in model_rows:
        if m["provider_id"] in name_for:
            model_ids_by_conn[name_for[m["provider_id"]]].add(m["model_id"])

    def keep_in_config(conn_name: str, model_id: str) -> None:
        entry = next(e for e in data["connections"] if isinstance(e, dict) and e.get("name") == conn_name)
        entry.setdefault("models", {})
        entry["models"] = entry["models"] or {}
        entry["models"].setdefault(model_id, {})

    aliases = data.setdefault("aliases", {}) or {}
    data["aliases"] = aliases
    auto, sel_provider, sel_model = _selection()
    selected_conn = name_for.get(sel_provider or "")
    if not auto and selected_conn and sel_model and "selected" not in aliases:
        aliases["selected"] = {"endpoint": endpoint_id(selected_conn, sel_model)}
        keep_in_config(selected_conn, sel_model)
    elif not auto and sel_provider and sel_model and selected_conn is None:
        notes.append(f"The model you had picked ({sel_model}) was on a connection that no longer exists, so "
                     "nothing is selected now. Choose a model on the Model Settings screen.")

    for pin in _read_pins(conn):
        if pin in aliases:
            continue
        matches = [c for c in model_ids_by_conn if pin in model_ids_by_conn[c]]
        if not matches:
            notes.append(f"A specialist or scheduled task asks for the model “{pin}”, which isn't set up on any "
                         "connection, so it will say so when it runs until you add that model.")
            continue
        chosen = selected_conn if selected_conn in matches else matches[0]
        if len(matches) > 1:
            labels = {e.get("name"): e.get("label") or e.get("name") for e in data["connections"] if isinstance(e, dict)}
            label = labels[chosen]
            others = ", ".join(labels[c] for c in matches if c != chosen)
            notes.append(f"The model “{pin}” that a specialist or scheduled task asks for is set up on more than one "
                         f"connection. Jarvis matched it to “{label}” (not {others}). You can change this in "
                         "models.yaml.")
        aliases[pin] = {"endpoint": endpoint_id(chosen, pin)}
        keep_in_config(chosen, pin)

    try:
        layer_config.save(data)
    except layer_config.ConfigError as err:
        raise ExportFailed(str(err)) from err

    # The last listing of every connection this move added, and what calls taught us.
    # A connection already in config keeps its own, newer listing.
    for old_id, name in created.items():
        listed = []
        for m in model_rows:
            if m["provider_id"] != old_id or m["source"] != "discovered":
                continue
            caps, pricing = _facts_to_capabilities(_facts(m))
            listed.append(Discovered(model_id=m["model_id"], label=m["label"], capabilities=caps, pricing=pricing))
        if listed:
            state.record_discovery(name, listed)
    for o in outcome_rows:
        name = created.get(o["provider_id"])
        if not name:
            continue
        eid = endpoint_id(name, o["model_id"])
        if o.get("ttft_ms"):
            state.record_latency(eid, int(o["ttft_ms"]), None)
        if o.get("last_ok_at") and not o.get("fail_streak"):
            state.record_success(eid)
        elif o.get("fail_streak"):
            state.restore_streak(eid, int(o["fail_streak"]), o.get("fail_message") or "")
    state.flush()

    _verify(entries, model_rows, created)
    return notes


def _verify(entries: list[dict[str, Any]], model_rows: list[dict[str, Any]], name_for: dict[str, str]) -> None:
    layer_config.forget()
    try:
        cfg = layer_config.current()
    except layer_config.ConfigError as err:
        raise ExportFailed(f"the new settings file didn't read back: {err}") from err
    state.reset()
    for entry in entries:
        conn = cfg.connections.get(entry["name"])
        if conn is None:
            raise ExportFailed(f"the connection “{entry['label']}” is missing after the move")
        for model_id in entry.get("models") or {}:
            if model_id not in conn.models:
                raise ExportFailed(f"the model “{model_id}” on “{entry['label']}” is missing after the move")
    for old_id, name in name_for.items():
        expected = {m["model_id"] for m in model_rows if m["provider_id"] == old_id and m["source"] == "discovered"}
        got = {d.model_id for d in state.discovered(name)}
        if expected - got:
            raise ExportFailed(f"{len(expected - got)} listed models on “{name}” are missing after the move")


def _tell(notes: list[str], *, failed: str | None = None) -> None:
    """Plain words for the person: in the state file (the settings screen reads it)
    and as a notification."""
    lines = list(notes)
    if failed:
        lines.insert(0, "Jarvis couldn't move your model connections to its new settings file, so nothing was "
                        f"changed and it will try again the next time it starts. What went wrong: {failed}")
    for line in lines:
        state.add_notice(line)
        logger.warning("model settings move: %s", line)
    if not lines:
        return
    try:
        from .. import notifications

        notifications.add(kind="system", level="warning" if failed else "info",
                          title="Model settings moved" if not failed else "Model settings couldn't be moved",
                          body="\n\n".join(lines))
    except Exception:  # noqa: BLE001 - telling must never break the move
        logger.exception("couldn't post the model settings notification")


def run(conn: sqlite3.Connection) -> bool:
    """Migration 35's step. True when the old tables may be dropped."""
    if not _tables_present(conn):
        return False
    try:
        notes = export(conn)
    except Exception as err:  # noqa: BLE001 - any failure leaves the tables and says so
        logger.exception("model settings export failed")
        _tell([], failed=str(err))
        return False
    _tell(notes)
    return True


def drop_tables(conn: sqlite3.Connection) -> None:
    for table in ("model_outcomes", *MODEL_TABLES, "model_providers"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")


def retry_if_pending() -> bool:
    """At startup: if an earlier move failed and left the old tables, try again —
    and drop them once it works. True when a move happened now."""
    from ..db import get_db

    db = get_db()
    if not _tables_present(db):
        return False
    if not run(db):
        return False
    db.execute("BEGIN")
    try:
        drop_tables(db)
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return True
