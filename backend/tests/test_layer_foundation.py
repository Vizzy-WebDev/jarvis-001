"""The model layer's foundation: request validation, the capability vocabulary and
its merge order, the catalog of connections/endpoints/aliases, and the errors."""

from __future__ import annotations

import logging

import pytest

from jarvis.models import capabilities, catalog, errors
from jarvis.models.prepared import Discovered
from jarvis.models.types import Hints, Message, OutputSpec, Request, Section, TextPart


def user(text: str) -> Message:
    return Message("user", (TextPart(text),))


def test_a_request_must_say_what_its_data_is():
    with pytest.raises(TypeError):
        Request(task_class="chat", items=(user("hi"),))  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        Request(task_class="chat", data_class="secret", items=(user("hi"),))  # type: ignore[arg-type]
    assert Request(task_class="chat", data_class="personal", items=(user("hi"),)).data_class == "personal"


def test_a_request_is_checked_for_what_cannot_be_honoured():
    with pytest.raises(ValueError):
        Request(task_class="", data_class="public", items=())
    with pytest.raises(ValueError):
        Request(task_class="x", data_class="public", items=(), output=OutputSpec(kind="json"))
    with pytest.raises(ValueError):
        Request(task_class="x", data_class="public", items=(),
                hints=Hints(stable_prefix_until="nope"), instructions=(Section("persona", "p"),))
    with pytest.raises(ValueError):
        Request(task_class="x", data_class="public", items=(), hints=Hints(reasoning_effort="max"))  # type: ignore[arg-type]


def test_the_vocabulary_is_fixed_and_extensions_are_namespaced():
    capabilities.check_name("tools")
    capabilities.check_name("x-someprovider:thing")
    with pytest.raises(capabilities.UnknownCapability):
        capabilities.check_name("vision")
    with pytest.raises(ValueError):
        capabilities.check_value("max_context_tokens", "big")
    with pytest.raises(ValueError):
        capabilities.check_value("tools", 1)


def test_probed_beats_discovered_beats_declared_and_conflicts_are_logged(caplog):
    with caplog.at_level(logging.INFO, logger="jarvis.models.capabilities"):
        merged = capabilities.merge(declared={"tools": True, "image_in": False, "json_mode": True},
                                    discovered={"tools": False, "image_in": True},
                                    probed={"tools": True}, where="c/m")
    assert merged["tools"] == capabilities.CapValue(True, "probed")
    assert merged["image_in"] == capabilities.CapValue(True, "discovered")
    assert merged["json_mode"] == capabilities.CapValue(True, "declared")
    assert "capability tools on c/m" in caplog.text


def _conn(name: str, **kw) -> catalog.Connection:
    return catalog.Connection(name=name, driver="fake", base_url="http://x", trust="local", **kw)


def test_two_connections_on_one_driver_with_overlapping_model_ids_are_separate_endpoints():
    a = _conn("box-a", models={"llama": catalog.ModelEntry(family="llama")})
    b = _conn("box-b")
    built = catalog.build(
        {"box-a": a, "box-b": b}, {},
        driver_defaults={"fake": {"text_in": True}}, quirk_caps={},
        discovered={"box-b": [Discovered("llama", capabilities={"tools": True}, family="llama")],
                    "box-a": [Discovered("llama", capabilities={"image_in": True})]},
        probed={})
    assert set(built.endpoints) == {"box-a/llama", "box-b/llama"}
    ea, eb = built.endpoints["box-a/llama"], built.endpoints["box-b/llama"]
    assert ea.configured and ea.listed and not eb.configured and eb.listed
    assert capabilities.has(ea.capabilities, "image_in") and not capabilities.has(ea.capabilities, "tools")
    assert capabilities.has(eb.capabilities, "tools")
    assert ea.family == eb.family == "llama"


def test_config_overrides_are_declared_and_context_becomes_a_capability():
    conn = _conn("c", quirks="noisy", models={"m": catalog.ModelEntry(capabilities={"tools": False},
                                                                     context=8000)})
    built = catalog.build({"c": conn}, {}, driver_defaults={"fake": {"tools": True}},
                          quirk_caps={"noisy": {"json_mode": True}}, discovered={}, probed={"c/m": {"tools": True}})
    caps = built.endpoints["c/m"].capabilities
    assert caps["max_context_tokens"] == capabilities.CapValue(8000, "declared")
    assert caps["json_mode"].value is True
    assert caps["tools"] == capabilities.CapValue(True, "probed")


def test_aliases_point_at_an_endpoint_or_a_family():
    conns = {"a": _conn("a", models={"m1": catalog.ModelEntry(family="f"), "m2": catalog.ModelEntry()}),
             "b": _conn("b", models={"m1": catalog.ModelEntry(family="f")})}
    aliases = {"one": catalog.Alias("one", endpoint="a/m2"), "fam": catalog.Alias("fam", family="f"),
               "gone": catalog.Alias("gone", endpoint="z/zz")}
    built = catalog.build(conns, aliases, driver_defaults={}, quirk_caps={}, discovered={}, probed={})
    assert [e.id for e in built.resolve_alias("one")] == ["a/m2"]
    assert [e.id for e in built.resolve_alias("fam")] == ["a/m1", "b/m1"]
    assert built.resolve_alias("gone") == [] and built.resolve_alias("nothing") == []


def test_endpoint_ids_split_on_the_first_slash():
    assert catalog.split_endpoint_id("router/vendor/model:free") == ("router", "vendor/model:free")
    with pytest.raises(ValueError):
        catalog.split_endpoint_id("nomodel")


def test_pricing_counts_cached_input_at_its_own_rate_and_unknown_is_not_zero():
    price = catalog.Pricing(input=3.0, output=15.0, cached_input=0.3)
    assert price.cost(input_tokens=1_000_000, output_tokens=0, cached_tokens=500_000) == pytest.approx(1.65)
    assert price.cost(input_tokens=None, output_tokens=None) is None
    assert catalog.Pricing(0, 0).free and not price.free


def test_every_error_type_says_whether_it_is_retryable():
    retryable = {cls.type for cls in errors.ALL if cls.retryable}
    assert retryable == {"rate_limited", "unavailable", "timeout"}
    assert set(errors.BY_TYPE) == {"rate_limited", "unavailable", "timeout", "auth", "invalid_request",
                                   "context_too_long", "content_refused", "no_eligible_endpoint",
                                   "budget_exceeded", "schema_validation_failed"}
    assert errors.Auth("no").at("c/m").endpoint_id == "c/m"
