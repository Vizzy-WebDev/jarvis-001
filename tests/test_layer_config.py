"""The model layer's config file, state file, and the one-time move out of the database."""

from __future__ import annotations

import json

import pytest
import yaml

from jarvis.models import config, state
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
             "models": {"m": {"capabilities": {"Bad Cap": True}, "pricing": {"input": -1, "output": 2}}}},
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
                   "Authorization: looks like a secret", "“Bad Cap” can't be a capability name", "pricing.input",
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


def test_the_shipped_defaults_name_no_effort_levels_for_any_provider_or_model():
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "intelligence" / "models" / "data" / "defaults.yaml").read_text(
        encoding="utf-8")
    code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    assert "reasoning_levels" not in code and "reasoning_default" not in code
    assert "listed_reasoning_levels" not in code
