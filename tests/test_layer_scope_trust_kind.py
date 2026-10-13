"""Phase 1 finish: failure scope and recovery, trust, endpoint kind, and the smaller items.

Each test pins one rule of the finished Model Fabric: where a failure's effect stops, who may
be trusted with what, which endpoints a generation call may consider, and the honesty fixes.
"""

from __future__ import annotations

import pytest

from jarvis import models
from jarvis.models import errors, execute, resolve, state
from jarvis.models.drivers import fake
from jarvis.models.types import Requirements, Tool

from layer_helpers import CHAT, ask, configure, fake_conn, layer  # noqa: F401


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(execute, "sleep", lambda s: None)


def two_models(**kw):
    return fake_conn("c", models={"m1": {"capabilities": CHAT}, "m2": {"capabilities": CHAT}}, **kw)


# --- scope: a 429 on one model leaves the others usable -----------------------------------------

def test_a_429_on_one_model_leaves_the_connections_other_models_usable(layer):
    configure(layer, [two_models()])
    fake.queue("c", errors.RateLimited("slow down", retry_after=300))
    fake.queue("c", fake.reply("from the other model"))
    response = models.generate(ask())
    assert response.provenance.endpoint_id == "c/m2"
    assert state.endpoint_rest("c/m1") and not state.rate_limited_until("c")
    assert models.explain_route(ask()).ranked == ("c/m2",)


def test_a_429_on_a_second_model_rests_the_whole_connection(layer):
    configure(layer, [two_models()])
    fake.queue("c", errors.RateLimited("slow", retry_after=300), errors.RateLimited("slow", retry_after=300))
    with pytest.raises(errors.RateLimited):
        models.generate(ask())
    assert [c.prepared.model_id for c in fake.calls()] == ["m1", "m2"]
    assert state.rate_limited_until("c")


def test_a_daily_limit_rests_the_endpoint_until_its_reset_not_for_a_few_seconds(layer):
    configure(layer, [two_models()])
    fake.queue("c", errors.RateLimited("daily allowance used up").with_scope("endpoint", "reset"))
    fake.queue("c", fake.reply("other"))
    models.generate(ask())
    assert state.endpoint_rest("c/m1")["kind"] == "reset"


# --- scope: the key / the account ---------------------------------------------------------------

@pytest.mark.parametrize("error", [errors.Auth("no"), errors.Billing("no credit")])
def test_a_key_or_billing_refusal_rests_the_connection_falls_back_under_auto_and_ends_a_pin(layer, error):
    configure(layer, [fake_conn("dead"), fake_conn("ok")], aliases={"selected": {"endpoint": "dead/m"}})
    fake.queue("dead", error)
    with pytest.raises(type(error)):  # pinned: the call ends on it
        models.generate(ask(requirements=Requirements(pin="selected")))
    assert state.connection_refused("dead")
    assert state.connection_refused_kind("dead") == error.type
    state.clear_rests("dead")
    fake.reset()
    fake.queue("dead", error)
    fake.queue("ok", fake.reply("fine"))
    assert models.generate(ask()).provenance.endpoint_id == "ok/m"  # Auto: falls back


def test_a_missing_model_falls_back_under_auto_and_is_skipped_until_discover(layer):
    configure(layer, [two_models()])
    fake.queue("c", errors.ModelNotFound("c.test has no model by that name."))
    fake.queue("c", fake.reply("the sibling still works"))
    response = models.generate(ask())
    assert response.provenance.endpoint_id == "c/m2"
    assert state.endpoint_rest("c/m1")["kind"] == "missing"
    assert not state.connection_refused("c")


def test_a_key_edit_clears_the_connection_rest_and_its_endpoint_breakers(layer):
    configure(layer, [two_models()])
    for _ in range(5):
        state.record_failure("c/m1", "down", threshold=3, base_s=60, max_s=600)
    state.mark_model_missing("c/m2", "gone")
    state.refuse_connection("c", "bad key", 600)
    assert state.resting_until("c/m1") and state.endpoint_rest("c/m2")
    state.clear_rests("c")
    assert not state.connection_refused("c")
    assert not state.resting_until("c/m1") and not state.endpoint_rest("c/m2")


def test_a_request_scoped_error_ends_the_call_even_under_auto(layer):
    configure(layer, [fake_conn("a"), fake_conn("b")])
    fake.queue("a", errors.InvalidRequest("bad request"))
    fake.queue("b", fake.reply("never"))
    with pytest.raises(errors.InvalidRequest):
        models.generate(ask())
    assert [c.connection for c in fake.calls()] == ["a"]


# --- trust --------------------------------------------------------------------------------------

def test_a_model_trust_override_applies_and_a_listing_never_raises_trust(layer):
    from jarvis.models import engine

    configure(layer, [fake_conn("gw", trust="standard", models={
        "pinned-up": {"capabilities": CHAT, "trust": "local"},
        "plain": {"capabilities": CHAT}}, upstream_trust={"acme": "zero_retention"})],
        policies={"data_classes": {"sensitive": ["local"]}})
    cat = engine.catalog()
    assert cat.endpoints["gw/pinned-up"].trust == "local"
    assert cat.endpoints["gw/plain"].trust == "standard"
    ranked = models.explain_route(ask(data_class="sensitive"))
    assert ranked.ranked == ("gw/pinned-up",)
    assert {r.reason for r in ranked.rejected} == {"trust_not_allowed"}


def test_a_listing_that_claims_zero_retention_does_not_raise_a_standard_connections_trust(layer):
    from jarvis.models import engine
    from jarvis.models.prepared import Discovered

    configure(layer, [fake_conn("gw", trust="standard", models={})])
    state.record_discovery("gw", [Discovered("vendor/x", upstream="vendor")])
    assert engine.catalog().endpoints["gw/vendor/x"].trust == "standard"


def test_free_is_a_separate_fact_from_trust(layer):
    from jarvis.models import engine

    configure(layer, [fake_conn("mine", trust="local"),                        # local: free by default
                      fake_conn("rented", trust="local", free=False),          # local but paid for
                      fake_conn("promo", trust="standard", free=True)])        # not private, but costs nothing
    cat = engine.catalog()
    assert cat.endpoints["mine/m"].pricing.free
    assert cat.endpoints["rented/m"].pricing is None
    assert cat.endpoints["promo/m"].pricing.free and cat.endpoints["promo/m"].trust == "standard"


# --- kind ---------------------------------------------------------------------------------------

def test_an_endpoint_that_is_not_a_chat_model_is_never_ranked_for_generation(layer):
    configure(layer, [fake_conn("c", models={"voice": {"capabilities": {**CHAT, "kind": "speech"}},
                                              "m": {"capabilities": CHAT}})])
    ranked = models.explain_route(ask())
    assert ranked.ranked == ("c/m",)
    assert [r.reason for r in ranked.rejected] == ["not_a_chat_model"]


# --- smaller items ------------------------------------------------------------------------------

def test_budget_spent_is_its_own_error_type(layer):
    configure(layer, [fake_conn("paid", trust="standard", models={"m": {"capabilities": CHAT}})],
              policies={"monthly_budget_usd": 1})
    state.add_spend(5)
    with pytest.raises(errors.BudgetExceeded):
        models.generate(ask())


def test_an_observed_context_window_expires_and_is_cleared_by_a_recheck(layer, monkeypatch):
    state.record_observed_context("c/m", 1000)
    assert state.observed_context("c/m")["max_context_tokens"] == 1000
    later = state.now() + 31 * 86400
    monkeypatch.setattr(state, "now", lambda: later)
    state.expire_observed(30)
    assert state.observed_context("c/m") is None
    monkeypatch.undo()
    state.record_observed_context("c/m", 800)
    state.clear_observed("c")
    assert state.observed_context("c/m") is None


def test_a_damaged_state_file_is_kept_aside_and_the_layer_starts_fresh(layer):
    path = layer.data_dir / "models_state.json"
    path.write_text("{ this is not json", encoding="utf-8")
    state.reset()
    assert state.snapshot()  # starts empty
    assert (layer.data_dir / "models_state.json.bak").read_text(encoding="utf-8") == "{ this is not json"
    assert any("damaged" in n["text"] for n in state.notices())


def test_a_tool_whose_parameters_are_an_array_is_ineligible(layer):
    configure(layer, [fake_conn("c")])
    ranked = models.explain_route(ask(tools=(Tool("t", "", {"type": "array", "items": {}}),)))
    assert ranked.ranked == ()
    assert [r.reason for r in ranked.rejected] == ["schema_not_expressible"]


def test_a_server_that_rejects_strict_is_not_reported_as_enforcing_it(layer):
    from jarvis.models.types import OutputSpec

    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    configure(layer, [fake_conn("c", models={"m": {"capabilities": {**CHAT, "structured_output_strict": True}}},
                                quirks="picky")],
              quirk_profiles={"picky": {"wire": {"rejects_strict": True}}})
    fake.queue("c", fake.reply('{"ok": true}'))
    response = models.generate(ask(output=OutputSpec("json", schema, "best_effort")))
    assert response.report.features["structured_output"] == "emulated"
    # and a caller that REQUIRES enforcement is told it can't have it, instead of being given a pretence
    with pytest.raises(errors.NoEligibleEndpoint):
        models.generate(ask(output=OutputSpec("json", schema, "required")))


def test_every_capability_has_a_plain_phrase_so_no_internal_name_is_ever_shown():
    from jarvis.models import capabilities

    for name in capabilities.FLAGS:
        assert name in resolve._CAN, name
        assert resolve.can_say(name) and "_" not in resolve.can_say(name)


# --- the boundary reaches the routing machinery ---------------------------------------------------

def test_explain_route_is_reachable_from_the_settings_api(layer):
    from fastapi.testclient import TestClient

    from jarvis import assembly
    from jarvis.main import create_app

    configure(layer, [fake_conn("c"), fake_conn("voice", models={"v": {"capabilities": {**CHAT, "kind": "speech"}}})])
    assembly.reset_for_tests()
    body = TestClient(create_app()).get("/api/models/route").json()
    assembly.reset_for_tests()
    assert body["ranked"] == ["c/m"] and body["pinned"] is False
    assert {r["reason"] for r in body["rejected"]} == {"not_a_chat_model"}


def test_a_routes_optimize_setting_changes_what_the_boundary_gets(layer):
    cheap = {"capabilities": CHAT}
    configure(layer, [fake_conn("dear", trust="standard", models={"m": {**cheap, "pricing": {"input": 9, "output": 9}}}),
                      fake_conn("cheap", trust="standard", models={"m": {**cheap, "pricing": {"input": 1, "output": 1}}})],
              routes={"chat": {"aliases": [], "allow_others": True, "optimize": "cost"}})
    assert models.explain_route(ask()).ranked[0] == "cheap/m"
