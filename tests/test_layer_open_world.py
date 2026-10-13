"""The Fabric is open-world: nothing it decides rests on a list of capabilities, kinds, providers
or models known today, and nothing it reports about one model is borrowed from another."""

from __future__ import annotations

import pytest

from jarvis import models
from jarvis.models import engine, errors, resolve
from jarvis.models import capabilities as caps_mod
from jarvis.models.drivers import fake
from jarvis.models.types import ImagePart, Message, Request, Requirements, TextPart

from layer_helpers import CHAT, ask, configure, fake_conn, layer  # noqa: F401


# --- a capability nobody has ever heard of -----------------------------------------------------

def test_a_capability_no_version_has_seen_can_be_declared_and_required_by_name(layer):
    configure(layer, [fake_conn("a", models={"plain": {"capabilities": CHAT},
                                              "gifted": {"capabilities": {**CHAT, "hologram_out": True}}})])
    ranked = models.explain_route(ask(requirements=Requirements(capabilities=frozenset({"hologram_out"}))))
    assert ranked.ranked == ("a/gifted",)
    assert [r.endpoint_id for r in ranked.rejected] == ["a/plain"]


def test_an_unknown_capability_nothing_has_is_said_by_its_own_name_not_a_generic_phrase(layer):
    configure(layer, [fake_conn("a")])
    ranked = models.explain_route(ask(requirements=Requirements(capabilities=frozenset({"hologram_out"}))))
    assert ranked.ranked == ()
    message = ranked.rejected[0].detail
    assert "hologram out" in message and "something this request needs" not in message


def test_an_unknown_capability_with_a_false_or_absent_value_never_counts_as_present(layer):
    configure(layer, [fake_conn("a", models={"m": {"capabilities": {**CHAT, "hologram_out": False}}})])
    assert models.explain_route(ask(requirements=Requirements(capabilities=frozenset({"hologram_out"})))).ranked == ()


# --- a kind of model nobody has heard of -------------------------------------------------------

def test_a_model_of_a_kind_nobody_listed_is_never_offered_a_chat_turn(layer):
    configure(layer, [fake_conn("a", models={"clip": {"capabilities": {**CHAT, "kind": "video"}},
                                              "m": {"capabilities": CHAT}})])
    ranked = models.explain_route(ask())
    assert ranked.ranked == ("a/m",)
    assert "video" in ranked.rejected[0].detail


def test_the_kind_a_gateway_reports_is_kept_as_reported(layer):
    from jarvis.models.drivers import openai_chat

    found = openai_chat._listed({"id": "x", "architecture": {"output_modalities": ["video"]}})
    assert found.capabilities["kind"] == "video"
    found = openai_chat._listed({"id": "y", "type": "rerank"})
    assert found.capabilities["kind"] == "rerank"


def test_an_input_kind_a_gateway_lists_becomes_a_capability_of_the_same_shape(layer):
    from jarvis.models.drivers import openai_chat

    found = openai_chat._listed({"id": "x", "architecture": {"input_modalities": ["text", "image", "audio", "video"]}})
    assert found.capabilities["audio_in"] is True and found.capabilities["video_in"] is True
    assert found.capabilities["image_in"] is True


# --- "Default" for effort is the model's own, never Jarvis's -----------------------------------

def test_a_guess_shipped_with_jarvis_is_never_shown_as_a_models_default(layer):
    from jarvis.models import config

    cfg = configure(layer, [{"name": "anth", "driver": "anthropic_messages", "base_url": "http://anth.test/v1",
                             "trust": "standard", "models": {"claude-x": {}}}])
    from jarvis.models import state
    from jarvis.models.prepared import Discovered

    state.record_discovery("anth", [Discovered("claude-x", capabilities={"reasoning_levels": ["low", "medium", "high"]})])
    endpoint = engine.catalog(cfg).endpoints["anth/claude-x"]
    assert caps_mod.reasoning_levels(endpoint.capabilities) == ("low", "medium", "high")
    assert caps_mod.reasoning_default(endpoint.capabilities) is None


@pytest.mark.parametrize("source", ["discovered", "configured"])
def test_a_default_that_a_server_listed_or_the_person_wrote_is_shown(layer, source):
    from jarvis.models import state
    from jarvis.models.prepared import Discovered

    caps = {"reasoning_levels": ["low", "high"], "reasoning_default": "low"}
    if source == "configured":
        cfg = configure(layer, [fake_conn("a", models={"m": {"capabilities": {**CHAT, **caps}}})])
    else:
        cfg = configure(layer, [fake_conn("a", models={})])
        state.record_discovery("a", [Discovered("m", capabilities={**CHAT, **caps})])
    endpoint = engine.catalog(cfg).endpoints["a/m"]
    assert caps_mod.reasoning_default(endpoint.capabilities) == "low"


def test_no_shipped_profile_names_a_default_level_for_a_whole_family():
    import yaml
    from pathlib import Path

    import jarvis.models as pkg

    doc = yaml.safe_load(open(Path(pkg.__path__[0]) / "data" / "defaults.yaml", encoding="utf-8"))
    for profile in doc.get("model_profiles", []):
        assert "reasoning_default" not in (profile.get("capabilities") or {}), profile
