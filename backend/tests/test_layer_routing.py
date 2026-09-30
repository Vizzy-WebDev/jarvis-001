"""Resolve and route: every rejection reason, the data policy, optimize, prefer/pin
through aliases, and affinity — all read through `explain_route`, which is the
same code path the real call takes (checked end to end in test_layer_execute)."""

from __future__ import annotations

from jarvis.models import engine, state
from jarvis.models.types import (Hints, ImagePart, Message, OutputSpec, Requirements, TextPart, Tool)

from layer_helpers import CHAT, ask, configure, fake_conn, layer  # noqa: F401

TOOLS = (Tool("look", "look something up", {"type": "object", "properties": {}}),)


def reasons(explanation) -> dict[str, str]:
    return {r.endpoint_id: r.reason for r in explanation.rejected}


def test_every_rejection_reason_is_recorded(layer, monkeypatch):
    from jarvis import config as secrets

    configure(layer, [
        fake_conn("nokey", secret_ref="missing_key", models={"m": {"capabilities": CHAT}}),
        fake_conn("down", models={"m": {"capabilities": CHAT}}),
        fake_conn("slow", models={"m": {"capabilities": CHAT}}),
        fake_conn("tired", models={"m": {"capabilities": CHAT}}),
        fake_conn("cloud", trust="standard", models={"m": {"capabilities": CHAT}}),
        fake_conn("notools", models={"m": {"capabilities": {"text_in": True}}}),
        fake_conn("tiny", models={"m": {"capabilities": CHAT, "context": 10}}),
        fake_conn("paid", models={"m": {"capabilities": CHAT, "pricing": {"input": 5, "output": 5}}}),
        fake_conn("unpriced", models={"m": {"capabilities": CHAT}}),
        fake_conn("strict", models={"m": {"capabilities": {**CHAT, "structured_output_strict": True}}}),
        fake_conn("odd", models={"m": {"capabilities": CHAT}}),
    ], policies={"data_classes": {"sensitive": ["local"]}})
    state.mark_connection_down("down", "no answer", 300)
    state.rate_limit("slow", 60)
    for _ in range(3):
        state.record_failure("tired/m", "boom", threshold=3, base_s=300, max_s=7200)

    base = ask("x" * 30, data_class="sensitive", tools=TOOLS)
    got = reasons(engine.explain(base))
    assert got["nokey/m"] == "no_key"
    assert got["down/m"] == "connection_unreachable"
    assert got["slow/m"] == "rate_limited"
    assert got["tired/m"] == "resting"
    assert got["cloud/m"] == "trust_not_allowed"
    assert got["notools/m"] == "missing_capability"
    assert got["tiny/m"] == "context_too_small"

    costly = ask("x", tools=TOOLS, requirements=Requirements(max_cost=0.000001))
    got = reasons(engine.explain(costly))
    assert got["paid/m"] == "over_max_cost" and got["unpriced/m"] == "price_unknown"

    pinned = ask(tools=TOOLS, requirements=Requirements(pin="only"))
    configure(layer, [fake_conn("a", models={"m": {"capabilities": CHAT}}),
                      fake_conn("b", models={"m": {"capabilities": CHAT}})], aliases={"only": {"endpoint": "a/m"}})
    assert reasons(engine.explain(pinned)) == {"b/m": "not_pinned"}

    configure(layer, [fake_conn("odd", models={"m": {"capabilities": CHAT}})])
    weird = ask(tools=(Tool("t", "", {"type": "object", "x-fake-inexpressible": True}),))
    assert reasons(engine.explain(weird)) == {"odd/m": "schema_not_expressible"}

    configure(layer, [fake_conn("paid", models={"m": {"capabilities": CHAT, "pricing": {"input": 1, "output": 1}}}),
                      fake_conn("free", models={"m": {"capabilities": CHAT, "pricing": {"input": 0, "output": 0}}})],
              policies={"monthly_budget_usd": 1})
    state.add_spend(1.5)
    assert reasons(engine.explain(ask())) == {"paid/m": "budget_spent"}
    assert engine.explain(ask()).ranked == ("free/m",)


def test_the_default_policy_never_excludes_an_endpoint_because_of_data_class(layer):
    configure(layer, [fake_conn("home", trust="local"), fake_conn("zr", trust="zero_retention"),
                      fake_conn("cloud", trust="standard")])
    for data_class in ("public", "personal", "sensitive"):
        explanation = engine.explain(ask(data_class=data_class))
        assert set(explanation.ranked) == {"home/m", "zr/m", "cloud/m"}, data_class
        assert explanation.rejected == ()


def test_a_configured_restriction_holds_even_against_a_pin(layer):
    configure(layer, [fake_conn("home", trust="local"), fake_conn("cloud", trust="standard")],
              aliases={"selected": {"endpoint": "cloud/m"}},
              policies={"data_classes": {"sensitive": ["local", "zero_retention"]}})
    explanation = engine.explain(ask(data_class="sensitive", requirements=Requirements(pin="selected")))
    assert explanation.ranked == ()
    assert reasons(explanation) == {"cloud/m": "trust_not_allowed", "home/m": "not_pinned"}
    # Other data classes are not restricted by it.
    assert engine.explain(ask(data_class="personal", requirements=Requirements(pin="selected"))).ranked == ("cloud/m",)


def test_images_and_tools_are_requirements_and_a_required_schema_needs_strict(layer):
    configure(layer, [fake_conn("blind", models={"m": {"capabilities": CHAT}}),
                      fake_conn("eyes", models={"m": {"capabilities": {**CHAT, "image_in": True}}}),
                      fake_conn("jsonish", models={"m": {"capabilities": {**CHAT, "json_mode": True}}}),
                      fake_conn("strict", models={"m": {"capabilities": {**CHAT, "structured_output_strict": True}}})])
    picture = ask()
    picture = type(picture)(**{**picture.__dict__, "items": (Message("user", (TextPart("what's this"),
                                                                                 ImagePart("image/png", "AAAA"))),)})
    assert engine.explain(picture).ranked == ("eyes/m",)

    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    required = engine.explain(ask(output=OutputSpec("json", schema, "required")))
    assert required.ranked == ("strict/m",) and required.emulated == ()
    lenient = engine.explain(ask(output=OutputSpec("json", schema, "best_effort")))
    assert set(lenient.ranked) == {"blind/m", "eyes/m", "jsonish/m", "strict/m"}
    assert set(lenient.emulated) == {"blind/m", "eyes/m", "jsonish/m"}


def test_optimize_changes_the_order(layer):
    configure(layer, [
        fake_conn("pricey", models={"m": {"pricing": {"input": 10, "output": 10}, "latency_ms": 100}}),
        fake_conn("cheap", models={"m": {"pricing": {"input": 1, "output": 1}, "latency_ms": 900}}),
        fake_conn("mid", models={"m": {"pricing": {"input": 5, "output": 5}, "latency_ms": 500}}),
    ])
    assert engine.explain(ask(optimize="quality")).ranked == ("pricey/m", "cheap/m", "mid/m")  # config order
    assert engine.explain(ask(optimize="cost")).ranked == ("cheap/m", "mid/m", "pricey/m")
    assert engine.explain(ask(optimize="speed")).ranked == ("pricey/m", "mid/m", "cheap/m")
    state.record_latency("cheap/m", 50, None)  # measured beats configured
    assert engine.explain(ask(optimize="speed")).ranked[0] == "cheap/m"


def test_ties_are_broken_by_the_other_two(layer):
    configure(layer, [fake_conn("a", models={"m": {"pricing": {"input": 1, "output": 1}, "latency_ms": 900}}),
                      fake_conn("b", models={"m": {"pricing": {"input": 1, "output": 1}, "latency_ms": 100}})])
    assert engine.explain(ask(optimize="cost")).ranked == ("b/m", "a/m")


def test_routes_prefer_and_pins_work_through_aliases(layer):
    configure(layer, [fake_conn("a"), fake_conn("b"), fake_conn("c", models={"m": {"family": "fam"}}),
                      fake_conn("d", models={"m": {"family": "fam"}})],
              aliases={"first": {"endpoint": "b/m"}, "fam": {"family": "fam"}, "only-a": {"endpoint": "a/m"}},
              routes={"chat": {"aliases": ["fam"], "allow_others": False},
                      "default": {"aliases": [], "allow_others": True}})
    assert engine.explain(ask()).ranked == ("c/m", "d/m")  # the route only
    assert engine.explain(ask(task_class="other")).ranked == ("a/m", "b/m", "c/m", "d/m")  # the default route
    assert engine.explain(ask(prefer=("first",))).ranked == ("b/m", "c/m", "d/m")
    assert engine.explain(ask(requirements=Requirements(pin="only-a"))).ranked == ()  # pinned outside the route
    assert engine.explain(ask(task_class="other", requirements=Requirements(pin="only-a"))).ranked == ("a/m",)


def test_affinity_sticks_while_eligible(layer):
    configure(layer, [fake_conn("a"), fake_conn("b")])
    engine.router.remember("session-1", "b/m")
    assert engine.explain(ask(affinity_key="session-1")).ranked == ("b/m", "a/m")
    assert engine.explain(ask(affinity_key="session-2")).ranked == ("a/m", "b/m")
    state.mark_connection_down("b", "gone", 300)
    assert engine.explain(ask(affinity_key="session-1")).ranked == ("a/m",)


def test_nothing_eligible_is_said_plainly(layer):
    configure(layer, [fake_conn("cloud", trust="standard")], aliases={"selected": {"endpoint": "cloud/m"}},
              policies={"data_classes": {"sensitive": ["local"]}})
    request = ask(data_class="sensitive", requirements=Requirements(pin="selected"))
    ranked, rejected = engine.route(request)
    err = engine.nothing_eligible(request, rejected, engine.catalog())
    assert err.type == "no_eligible_endpoint"
    assert "privacy settings" in str(err) and "sensitive" in str(err)
    missing = ask(requirements=Requirements(pin="nope"))
    assert "isn't set up" in str(engine.nothing_eligible(missing, [], engine.catalog()))


def test_hints_do_not_change_eligibility(layer):
    configure(layer, [fake_conn("a")])
    assert engine.explain(ask(hints=Hints(reasoning_effort="high", max_output_tokens=10))).ranked == ("a/m",)
