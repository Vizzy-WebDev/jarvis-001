"""The one-time move of the old model tables into the config and state files.

Run by migration 35 with the migration's own database connection (never
`get_db()`, which is still opening). It exports, reads both files back and checks
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
from typing import Any

from . import config as layer_config
from . import state
from .catalog import Pricing, endpoint_id
from .prepared import Discovered

logger = logging.getLogger(__name__)

TABLES = ("model_outcomes", "provider_models", "model_providers")

_DRIVER_FOR_FORMAT = {
    "openai-responses": "openai_responses",
    "openai-chat": "openai_chat",
    "anthropic-messages": "anthropic_messages",
    "gemini-generatecontent": "gemini_generate",
}
#: The old Kind table's default addresses — used here once, for rows that stored none.
_OLD_DEFAULT_ADDRESS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "ollama": "http://127.0.0.1:11434/v1",
    "lmstudio": "http://127.0.0.1:1234/v1",
}
_LOCAL_KINDS = {"ollama": "ollama", "lmstudio": "lmstudio"}


class ExportFailed(RuntimeError):
    pass


def _tables_present(conn: sqlite3.Connection) -> bool:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name IN (?, ?, ?)",
                        TABLES).fetchall()
    return len(rows) == len(TABLES)


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
    conn_rows = [dict(r) for r in conn.execute("SELECT * FROM model_providers ORDER BY created_at, id")]
    model_rows = [dict(r) for r in conn.execute("SELECT * FROM provider_models ORDER BY provider_id, added_at")]
    outcome_rows = [dict(r) for r in conn.execute("SELECT * FROM model_outcomes")]
    notes: list[str] = []

    data = layer_config.raw()
    existing = {c.get("name") for c in data.get("connections") or [] if isinstance(c, dict)}
    taken = set(existing)
    name_for: dict[str, str] = {}
    entries: list[dict[str, Any]] = []
    for row in conn_rows:
        driver = _DRIVER_FOR_FORMAT.get(row["format"])
        if driver is None:
            notes.append(f"Your connection “{row['label']}” used a format Jarvis no longer knows "
                         f"({row['format']}), so it wasn't moved.")
            continue
        name = layer_config.unique_name(row["label"] or row["kind"], taken)
        taken.add(name)
        name_for[row["id"]] = name
        mine = [m for m in model_rows if m["provider_id"] == row["id"]]
        has_rich_listing = any(any(k in _facts(m) for k in ("chat", "tools", "image", "free", "router"))
                               for m in mine)
        entry: dict[str, Any] = {
            "name": name,
            "label": row["label"],
            "preset": row["kind"],
            "driver": driver,
            "base_url": row["base_url"] or _OLD_DEFAULT_ADDRESS.get(row["kind"], ""),
            "trust": "local" if row["kind"] in _LOCAL_KINDS else "standard",
        }
        if row["secret_ref"]:
            entry["secret_ref"] = row["secret_ref"]
        quirks = _LOCAL_KINDS.get(row["kind"]) or ("gateway" if driver == "openai_chat" and has_rich_listing else None)
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
        entry = next(e for e in entries if e["name"] == conn_name)
        entry.setdefault("models", {}).setdefault(model_id, {})

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
            label = next(e["label"] for e in entries if e["name"] == chosen)
            others = ", ".join(next(e["label"] for e in entries if e["name"] == c) for c in matches if c != chosen)
            notes.append(f"The model “{pin}” that a specialist or scheduled task asks for is set up on more than one "
                         f"connection. Jarvis matched it to “{label}” (not {others}). You can change this in "
                         "models.yaml.")
        aliases[pin] = {"endpoint": endpoint_id(chosen, pin)}
        keep_in_config(chosen, pin)

    try:
        layer_config.save(data)
    except layer_config.ConfigError as err:
        raise ExportFailed(str(err)) from err

    # The last listing of every connection, and what calls taught us.
    for old_id, name in name_for.items():
        listed = []
        for m in model_rows:
            if m["provider_id"] != old_id or m["source"] != "discovered":
                continue
            caps, pricing = _facts_to_capabilities(_facts(m))
            listed.append(Discovered(model_id=m["model_id"], label=m["label"], capabilities=caps, pricing=pricing))
        if listed:
            state.record_discovery(name, listed)
    for o in outcome_rows:
        name = name_for.get(o["provider_id"])
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

    _verify(entries, model_rows, name_for)
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
    for table in TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
