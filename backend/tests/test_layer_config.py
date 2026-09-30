"""The model layer's config file, state file, and the one-time move out of the database."""

from __future__ import annotations

import json

import pytest
import yaml

from jarvis.models import config, migrate_db, state
from jarvis.models.prepared import Discovered


@pytest.fixture
def layer(scratch):
    config.forget()
    state.reset()
    yield scratch
    config.forget()
    state.reset()


def write(scratch, doc: dict) -> None:
    (scratch.data_dir / "models.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")


def conn(name: str = "box", **kw) -> dict:
    return {"name": name, "driver": "fake", "base_url": "http://127.0.0.1:9", "trust": "local", **kw}


# --- config ---------------------------------------------------------------------------------

def test_no_file_means_no_connections_and_the_shipped_defaults(layer):
    cfg = config.current()
    assert cfg.connections == {} and cfg.aliases == {}
    assert cfg.route_for("anything").allow_others is True
    assert cfg.policy.data_classes == {}  # unrestricted by default
    assert {p.id for p in cfg.presets} >= {"openai", "anthropic", "gemini", "openrouter", "ollama", "lmstudio", "custom"}


def test_every_problem_is_reported_at_once_and_says_where(layer):
    write(layer, {
        "connections": [
            {"name": "Bad Name", "driver": "fake", "base_url": "x", "trust": "local"},
            {"name": "a", "driver": "nope", "base_url": "http://x", "trust": "local"},
            {"name": "b", "driver": "fake", "base_url": "ftp://x", "trust": "public",
             "default_params": {"headers": {"Authorization": "Bearer sk-123"}},
             "models": {"m": {"capabilities": {"vision": True}, "pricing": {"input": -1, "output": 2}}}},
        ],
        "aliases": {"x": {"endpoint": "ghost/m"}, "y": {}},
        "routes": {"chat": {"aliases": ["missing"], "optimize": "fastest"}},
        "policies": {"data_classes": {"sensitive": ["cloud"]}},
        "surprise": 1,
    })
    with pytest.raises(config.ConfigError) as caught:
        config.current()
    text = str(caught.value)
    for needle in ("connections[0].name", "connections.a.driver", "connections.b.base_url", "connections.b.trust",
                   "Authorization: looks like a secret", "“vision” isn't a capability", "pricing.input",
                   "aliases.y", "routes.chat.aliases", "routes.chat.optimize",
                   "policies.data_classes.sensitive", "surprise"):
        assert needle in text, needle


def test_a_quirk_flag_the_driver_does_not_understand_is_refused(layer):
    write(layer, {"quirk_profiles": {"odd": {"wire": {"teleport": True}}},
                  "connections": [{**conn(), "driver": "openai_chat", "quirks": "odd"}]})
    with pytest.raises(config.ConfigError, match="teleport"):
        config.current()


def test_a_good_file_parses_into_connections_aliases_routes_and_policy(layer):
    write(layer, {
        "connections": [conn("box-a", limits={"concurrency": 2, "rpm": 30},
                             models={"llama": {"family": "llama", "pricing": {"input": 0, "output": 0}, "context": 8192}}),
                        conn("box-b", trust="standard", secret_ref="model_box_b")],
        "aliases": {"fast": {"family": "llama"}, "main": {"endpoint": "box-a/llama"}},
        "routes": {"chat": {"aliases": ["main", "fast"], "allow_others": False, "optimize": "cost"}},
        "policies": {"data_classes": {"sensitive": ["local"]}, "monthly_budget_usd": 5},
        "embedding_spaces": {"notes": {"primary": "box-a/emb", "backups": ["box-b/emb"], "dimension": 768}},
        "settings": {"retries": 1},
    })
    cfg = config.current()
    assert cfg.connections["box-a"].limits.concurrency == 2
    assert cfg.connections["box-a"].models["llama"].pricing.free
    assert cfg.route_for("chat").aliases == ("main", "fast") and cfg.route_for("chat").optimize == "cost"
    assert cfg.route_for("other").allow_others
    assert cfg.policy.data_classes == {"sensitive": frozenset({"local"})}
    assert cfg.policy.monthly_budget_usd == 5
    assert cfg.embedding_spaces["notes"].backups == ("box-b/emb",)
    assert cfg.settings.retries == 1 and cfg.settings.repair_attempts == 2


def test_edits_validate_before_touching_the_disk_and_a_hand_edit_is_picked_up(layer):
    config.add_connection(conn("box"))
    config.set_model("box", "m1", {"label": "Model one"})
    config.set_alias("selected", endpoint="box/m1")
    assert config.current().aliases["selected"].endpoint == "box/m1"
    before = (layer.data_dir / "models.yaml").read_text(encoding="utf-8")
    with pytest.raises(config.ConfigError):
        config.update_connection("box", trust="everyone")
    assert (layer.data_dir / "models.yaml").read_text(encoding="utf-8") == before

    doc = yaml.safe_load(before)
    doc["connections"][0]["label"] = "Edited by hand"
    (layer.data_dir / "models.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    import os
    import time
    os.utime(layer.data_dir / "models.yaml", (time.time() + 5, time.time() + 5))
    assert config.current().connections["box"].label == "Edited by hand"

    config.remove_alias("selected")
    config.remove_connection("box")
    assert config.current().connections == {}


def test_unique_names_are_slugs_without_slashes():
    assert config.unique_name("My Ollama / Desk", set()) == "my-ollama-desk"
    assert config.unique_name("box", {"box", "box-2"}) == "box-3"


# --- state ----------------------------------------------------------------------------------

def test_a_failed_discovery_keeps_the_last_listing(layer):
    state.record_discovery("box", [Discovered("m1"), Discovered("m2", capabilities={"tools": True})])
    state.record_discovery("box", None, error="didn't answer")
    assert [d.model_id for d in state.discovered("box")] == ["m1", "m2"]
    assert state.discovery_status("box")["ok"] is False
    state.flush()
    state.reset()
    assert state.discovered("box")[1].capabilities == {"tools": True}


def test_the_breaker_rests_an_endpoint_after_repeated_failures_and_an_answer_ends_it(layer, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(state, "now", lambda: clock[0])
    for _ in range(2):
        state.record_failure("box/m", "down", threshold=3, base_s=300, max_s=7200)
    assert state.resting_until("box/m") is None
    state.record_failure("box/m", "down", threshold=3, base_s=300, max_s=7200)
    assert state.resting_until("box/m") == 1300.0
    state.record_failure("box/m", "down", threshold=3, base_s=300, max_s=7200)
    assert state.resting_until("box/m") == 1600.0  # doubled
    clock[0] = 2000.0
    assert state.resting_until("box/m") is None  # rest over: eligible again
    state.record_success("box/m")
    assert state.health("box/m")["streak"] == 0


def test_spend_rolls_over_with_the_month(layer, monkeypatch):
    clock = [1_780_000_000.0]
    monkeypatch.setattr(state, "now", lambda: clock[0])
    state.add_spend(1.25)
    state.add_spend(0.25)
    assert state.month_spend() == pytest.approx(1.5)
    clock[0] += 40 * 86400
    assert state.month_spend() == 0.0


# --- the move out of the database -----------------------------------------------------------

def _old_tables(db):
    """The model tables as they were before migration 35 (which drops them on a
    fresh database), recreated from their original DDL."""
    from jarvis.db import _split_sql
    from jarvis.migrations_extra import EXTRA_MIGRATION_SQL

    for version in (27, 28, 29):
        for script in EXTRA_MIGRATION_SQL[version]:
            for statement in _split_sql(script):
                db.execute(statement)


def _seed_old_tables(db, *, with_selection=True):
    _old_tables(db)
    db.execute("INSERT INTO model_providers (id, kind, format, label, base_url, secret_ref, created_at) VALUES "
               "('prov_1', 'ollama', 'openai-chat', 'Ollama', NULL, NULL, '2026-01-01'),"
               "('prov_2', 'custom', 'openai-chat', 'My Gateway', 'http://gw.local/v1', 'model_prov_2', '2026-01-02'),"
               "('prov_3', 'anthropic', 'anthropic-messages', 'Anthropic', NULL, 'model_prov_3', '2026-01-03')")
    rows = [("prov_1", "llama3", None, "discovered", None),
            ("prov_1", "shared-model", None, "discovered", None),
            ("prov_1", "typed-in", "Typed", "manual", None),
            ("prov_2", "shared-model", None, "discovered", json.dumps({"tools": True, "free": True, "image": False})),
            ("prov_3", "claude-x", "Claude X", "discovered",
             json.dumps({"maxOutput": 64000, "effort": {"levels": ["low", "high"]}}))]
    for provider, model, label, source, facts in rows:
        db.execute("INSERT INTO provider_models (provider_id, model_id, label, source, facts_json, added_at, "
                   "last_seen_at) VALUES (?, ?, ?, ?, ?, '2026-01-05', '2026-01-05')",
                   (provider, model, label, source, facts))
    db.execute("INSERT INTO model_outcomes (provider_id, model_id, last_ok_at, ttft_ms, fail_streak) "
               "VALUES ('prov_1', 'llama3', '2026-01-06', 900, 0)")
    db.execute("INSERT INTO model_outcomes (provider_id, model_id, last_fail_at, fail_message, fail_streak) "
               "VALUES ('prov_3', 'claude-x', '2026-01-06', 'no credit', 2)")
    if with_selection:
        from jarvis import prefs
        prefs.set_prefs({"selectedAuto": False, "selectedProviderId": "prov_3", "selectedModelId": "claude-x",
                         "selectedEffort": "high"})


def test_the_old_tables_move_into_config_and_state_and_pins_become_aliases(layer):
    from jarvis.db import get_db

    db = get_db()
    _seed_old_tables(db)
    db.execute("INSERT INTO agents (id, name, model_pin, capability_access, collaborators, created_at, updated_at) "
               "VALUES ('ag_1', 'Pinned', 'shared-model', '{}', '[]', '2026-01-01', '2026-01-01')")

    assert migrate_db.run(db) is True
    cfg = config.current()
    names = set(cfg.connections)
    assert names == {"ollama", "my-gateway", "anthropic"}
    ollama = cfg.connections["ollama"]
    assert ollama.trust == "local" and ollama.quirks == "ollama" and ollama.base_url == "http://127.0.0.1:11434/v1"
    assert "typed-in" in ollama.models  # typed by hand: kept in config
    gw = cfg.connections["my-gateway"]
    assert gw.trust == "standard" and gw.secret_ref == "model_prov_2" and gw.quirks == "gateway"
    assert cfg.connections["anthropic"].driver == "anthropic_messages"
    assert cfg.connections["anthropic"].base_url == "https://api.anthropic.com/v1"

    assert cfg.aliases["selected"].endpoint == "anthropic/claude-x"
    assert "claude-x" in cfg.connections["anthropic"].models  # the chosen model stays whatever discovery says
    # The pin matched two connections; neither is the selected one, so the first was used, and said so.
    assert cfg.aliases["shared-model"].endpoint == "ollama/shared-model"
    notices = " ".join(n["text"] for n in state.notices())
    assert "“shared-model”" in notices and "“Ollama”" in notices and "My Gateway" in notices

    listed = {d.model_id: d for d in state.discovered("my-gateway")}
    assert listed["shared-model"].capabilities == {"tools": True, "image_in": False}
    assert listed["shared-model"].pricing.free
    claude = state.discovered("anthropic")[0]
    assert claude.capabilities == {"max_output_tokens": 64000, "reasoning_control": True}
    assert state.latency_ms("ollama/llama3") == 900
    assert state.health("anthropic/claude-x")["streak"] == 2

    from jarvis import notifications
    assert any("Model settings moved" == n["title"] for n in notifications.listed())

    migrate_db.drop_tables(db)
    assert not migrate_db._tables_present(db)


def test_a_failed_export_leaves_the_tables_and_says_so(layer, monkeypatch):
    from jarvis.db import get_db

    db = get_db()
    _seed_old_tables(db, with_selection=False)

    def broken(_data):
        raise config.ConfigError(["the disk is full"])

    monkeypatch.setattr(config, "save", broken)
    assert migrate_db.run(db) is False
    assert migrate_db._tables_present(db)
    assert db.execute("SELECT COUNT(*) FROM model_providers").fetchone()[0] == 3
    assert any("couldn't move your model connections" in n["text"] and "disk is full" in n["text"]
               for n in state.notices())


def test_nothing_to_move_is_not_an_error(layer):
    from jarvis.db import get_db

    _old_tables(get_db())
    assert migrate_db.run(get_db()) is True  # tables exist but are empty
    assert config.current().connections == {}


def test_a_database_whose_migration_35_was_another_branchs_is_still_moved_at_startup(layer):
    """Found on a real install: another branch's migration 35 had renamed
    provider_models to provider_catalog, so this build's migration 35 never ran.
    Startup moves whatever is left, from either table name."""
    from jarvis.db import get_db

    db = get_db()
    _seed_old_tables(db, with_selection=False)
    db.execute("ALTER TABLE provider_models RENAME TO provider_catalog")
    assert migrate_db.retry_if_pending() is True
    assert set(config.current().connections) == {"ollama", "my-gateway", "anthropic"}
    assert {d.model_id for d in state.discovered("my-gateway")} == {"shared-model"}
    assert not migrate_db._tables_present(db)
    assert migrate_db.retry_if_pending() is False  # nothing left: a no-op from now on


def test_a_service_already_set_up_again_is_not_duplicated_and_keeps_its_own_listing(layer):
    from jarvis.db import get_db

    config.add_connection({"name": "gw-new", "label": "Gateway (re-added)", "driver": "openai_chat",
                           "base_url": "http://gw.local/v1", "trust": "standard", "secret_ref": "model_gw_new"})
    state.record_discovery("gw-new", [Discovered("fresh-model")])
    db = get_db()
    _seed_old_tables(db, with_selection=False)
    assert migrate_db.retry_if_pending() is True
    names = set(config.current().connections)
    assert "my-gateway" not in names and "gw-new" in names  # not added a second time
    assert [d.model_id for d in state.discovered("gw-new")] == ["fresh-model"]  # its newer listing kept
    assert any("“My Gateway” wasn't added again" in n["text"] and "key is still saved" in n["text"]
               for n in state.notices())
