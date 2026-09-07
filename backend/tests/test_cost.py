"""Cost tracking: measured, provider-reported and calculated, kept apart.

The load-bearing test here is the end-to-end one — a real adapter parsing a real
provider's usage chunk off a real socket, through the real gateway, into a real
row. Every layer of that path previously discarded the number, and a unit test
of the store alone would not have noticed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from jarvis import conversation
from jarvis.adapters import usage as usage_read
from jarvis.cost import advisor, prices, report, store
from jarvis.db import reset_for_tests as reset_db
from jarvis.events import EventType
from jarvis.events.bus import Event, EventBus
from jarvis.gateway import availability, connections, registry
from jarvis.gateway.client import Gateway
from jarvis.jscompat import to_iso_z
from jarvis.observers.cost import record_model_call
from jarvis.orchestrator.model_port import StepComplete

from stub_openai_server import StubModelServer


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    advisor.reset_cache()
    conversation.reset_for_tests()
    availability.reset_for_tests()
    yield
    availability.reset_for_tests()
    conversation.reset_for_tests()
    advisor.reset_cache()
    reset_db()


# --- reading what the provider said ------------------------------------------

def test_a_number_the_provider_did_not_report_is_absent_not_zero():
    """Zero is a claim; absence is the truth. Blurring them is how a subsystem
    built to avoid invented numbers starts inventing them."""
    class OnlyInput:
        prompt_tokens = 40

    assert usage_read.from_openai(OnlyInput()) == {"unitsIn": 40}
    assert usage_read.from_openai(None) is None
    assert usage_read.from_anthropic(object()) is None


def test_each_provider_shape_reads_into_the_same_three_fields():
    class OpenAI:
        prompt_tokens, completion_tokens = 10, 5
        prompt_tokens_details = type("D", (), {"cached_tokens": 2})()

    class Anthropic:
        input_tokens, output_tokens, cache_read_input_tokens = 10, 5, 2

    class Gemini:
        prompt_token_count, candidates_token_count, cached_content_token_count = 10, 5, 2

    expected = {"unitsIn": 10, "unitsOut": 5, "cachedIn": 2}
    assert usage_read.from_openai(OpenAI()) == expected
    assert usage_read.from_anthropic(Anthropic()) == expected
    assert usage_read.from_gemini(Gemini()) == expected


# --- the observer seam --------------------------------------------------------

def test_a_step_notification_without_usage_records_nothing():
    """The orchestrator publishes MODEL_CALL_COMPLETED once per step to say a
    step finished, and the gateway publishes it again with the real numbers.
    Recording both would double every turn — and the doubled figure would look
    entirely plausible."""
    record_model_call(Event(type=EventType.MODEL_CALL_COMPLETED,
                            payload={"modelId": "m", "provider": "openai", "step": 1}))
    assert store.list_events_since("1970-01-01T00:00:00.000Z") == []


def test_usage_with_no_provider_is_not_attributed_to_a_guess():
    record_model_call(Event(type=EventType.MODEL_CALL_COMPLETED,
                            payload={"modelId": "m", "usage": {"unitsIn": 5}}))
    assert store.list_events_since("1970-01-01T00:00:00.000Z") == []


def test_the_observer_records_what_the_gateway_published():
    record_model_call(Event(type=EventType.MODEL_CALL_COMPLETED, payload={
        "provider": "openai", "model": "gpt-x", "sessionId": "s1", "background": True,
        "usage": {"unitsIn": 10, "unitsOut": 5, "cachedIn": 2}}))
    events = store.list_events_since("1970-01-01T00:00:00.000Z")
    assert len(events) == 1
    assert (events[0]["provider"], events[0]["modelId"]) == ("openai", "gpt-x")
    assert (events[0]["unitsIn"], events[0]["unitsOut"], events[0]["cachedIn"]) == (10, 5, 2)
    assert events[0]["background"] is True


# --- end to end, over a real socket -------------------------------------------

def test_a_real_turn_records_the_usage_the_provider_actually_sent():
    stub = StubModelServer()
    base_url = stub.start()
    try:
        conn = connections.add_connection(adapter="openai-compatible", base_url=base_url,
                                          label="stub", provider="custom", kind="local",
                                          key_required=False)
        model = registry.add_model(connection_id=conn["id"], model="stub-model")
        stub.says("done")

        bus = EventBus()
        bus.subscribe(EventType.MODEL_CALL_COMPLETED, record_model_call)
        events = list(Gateway(event_bus=bus).stream(
            messages=[{"role": "user", "text": "hello"}], system="", tools=[],
            session_id="s1"))
    finally:
        stub.stop()

    step = [e for e in events if isinstance(e, StepComplete)][0]
    # The empty-`choices` usage chunk really was parsed, by the real adapter.
    assert step.usage == {"unitsIn": 11, "unitsOut": 3, "cachedIn": 4}

    recorded = store.list_events_since("1970-01-01T00:00:00.000Z")
    assert len(recorded) == 1
    assert recorded[0]["unitsIn"] == 11 and recorded[0]["unitsOut"] == 3
    assert recorded[0]["modelId"] == "stub-model"
    assert recorded[0]["sessionId"] == "s1"
    assert model["id"]


# --- prices: never a guess ----------------------------------------------------

def test_an_unpriced_model_has_no_money_figure_at_all():
    assert prices.calculate(provider="openai", model_id="unpriced",
                            units_in=1000, units_out=1000) is None


def test_a_price_on_record_is_multiplied_by_the_real_count():
    prices.set_user_price(provider="openai", model_id="m", price_in=0.001, price_out=0.002)
    money = prices.calculate(provider="openai", model_id="m", units_in=1000, units_out=500)
    assert money["amount"] == pytest.approx(1.0 + 1.0)
    assert money["priceSource"] == "user"


def test_a_user_price_is_never_replaced_by_a_provider_reported_one():
    prices.set_user_price(provider="openrouter", model_id="a/b", price_in=9.0, price_out=9.0)
    result = prices.refresh_from_openrouter(
        fetch=lambda: {"data": [{"id": "a/b", "pricing": {"prompt": "0.001", "completion": "0.002"}},
                                {"id": "c/d", "pricing": {"prompt": "0.003", "completion": "0.004"}}]})
    assert result["ok"] and result["updated"] == 1
    assert store.get_price("openrouter", "a/b")["priceIn"] == 9.0
    assert store.get_price("openrouter", "c/d")["source"] == "provider_reported"


def test_a_failed_refresh_is_reported_not_raised():
    def boom():
        raise RuntimeError("network is down")

    assert prices.refresh_from_openrouter(fetch=boom) == {
        "ok": False, "updated": 0, "error": "network is down"}


# --- the report keeps its three kinds of number apart -------------------------

def test_unpriced_usage_is_counted_and_named_rather_than_costed_at_zero():
    store.record_event(provider="openai", model_id="priced", unit_kind="tokens",
                       units_in=1000, units_out=0)
    store.record_event(provider="openai", model_id="unpriced", unit_kind="tokens",
                       units_in=1000, units_out=0)
    prices.set_user_price(provider="openai", model_id="priced", price_in=0.001, price_out=0.0)

    data = report.usage_breakdown("1970-01-01T00:00:00.000Z")
    assert data["calculated"]["amount"] == pytest.approx(1.0)
    assert [g["modelId"] for g in data["pricelessGroups"]] == ["unpriced"]
    assert {g["modelId"] for g in data["measured"]["groups"]} == {"priced", "unpriced"}


def test_nothing_priced_at_all_gives_no_money_figure_rather_than_zero():
    store.record_event(provider="openai", model_id="m", unit_kind="tokens", units_in=5)
    assert report.usage_breakdown("1970-01-01T00:00:00.000Z")["calculated"] is None


def test_a_period_only_counts_what_falls_inside_it():
    store.record_event(provider="openai", model_id="m", unit_kind="tokens", units_in=5)
    future = to_iso_z(datetime.now(timezone.utc) + timedelta(days=1))
    assert report.usage_breakdown(future)["eventCount"] == 0
    assert report.today()["eventCount"] == 1


def test_most_used_is_a_real_call_count():
    for _ in range(3):
        store.record_event(provider="openai", model_id="often", unit_kind="tokens", units_in=1)
    store.record_event(provider="openai", model_id="rarely", unit_kind="tokens", units_in=1)
    assert report.most_used("1970-01-01T00:00:00.000Z")["modelId"] == "often"


# --- cost at decision time ----------------------------------------------------

def test_no_price_on_record_means_no_opinion_rather_than_a_fallback_number():
    assert advisor.observed_cost_tier("openai", "nothing-known") is None
    assert advisor.observed_cost_tier(None, "m") is None


@pytest.mark.parametrize("price_in,price_out,expected", [
    (0.0, 0.0, 0),              # a genuinely free/local model IS bucket 0, not unknown
    (0.0000002, 0.0000003, 0),
    (0.000001, 0.000002, 1),
    (0.000005, 0.000010, 2),
    (0.00002, 0.00003, 3),
    (0.001, 0.002, 4),
])
def test_a_measured_price_lands_in_the_same_zero_to_four_domain_the_guess_used(
        price_in, price_out, expected):
    """The whole safety argument for substituting this into the router's scoring
    is that the domain is unchanged, so every weight stays as tuned."""
    prices.set_user_price(provider="p", model_id="m", price_in=price_in, price_out=price_out)
    advisor.reset_cache()
    tier = advisor.observed_cost_tier("p", "m")
    assert tier == expected
    assert 0 <= tier <= 4


def test_the_router_prefers_the_measured_price_over_the_catalogs_guess():
    from jarvis.gateway.routing import Task, build_candidates

    entries = [
        {"id": "guessed-cheap", "enabled": True, "provider": "p", "model": "cheap",
         "adapter": "openai-compatible", "keyRequired": False, "kind": "local",
         "caps": {"tools": True}, "tier": {"speed": 3, "quality": 3, "cost": 0}},
        {"id": "guessed-dear", "enabled": True, "provider": "p", "model": "dear",
         "adapter": "openai-compatible", "keyRequired": False, "kind": "local",
         "caps": {"tools": True}, "tier": {"speed": 3, "quality": 3, "cost": 4}},
    ]
    task = Task(text="hello", background=True)
    assert [e["id"] for e in build_candidates(task, entries=entries)][0] == "guessed-cheap"

    # Now measure the opposite of what the names suggested.
    prices.set_user_price(provider="p", model_id="cheap", price_in=0.001, price_out=0.002)
    prices.set_user_price(provider="p", model_id="dear", price_in=0.0, price_out=0.0)
    advisor.reset_cache()
    assert [e["id"] for e in build_candidates(task, entries=entries)][0] == "guessed-dear"


# --- balances stay separate ---------------------------------------------------

def test_a_provider_balance_is_reported_separately_from_measured_usage():
    store.record_event(provider="openrouter", model_id="a/b", unit_kind="tokens", units_in=10)
    store.record_balance("openrouter", {"remaining": 4.2, "currency": "USD"})
    data = report.usage_breakdown("1970-01-01T00:00:00.000Z")
    assert data["providerReported"][0]["detail"]["remaining"] == 4.2
    # ...and never folded into the calculated total.
    assert data["calculated"] is None


def test_only_the_latest_balance_reading_is_kept():
    store.record_balance("openrouter", {"remaining": 10.0})
    store.record_balance("openrouter", {"remaining": 3.0})
    assert len(store.list_balances()) == 1
    assert store.get_balance("openrouter")["detail"]["remaining"] == 3.0


def test_the_balance_timer_stays_off_without_its_interlock(monkeypatch):
    from jarvis.cost import balances

    monkeypatch.delenv(balances.ENABLE_ENV, raising=False)
    assert balances.start() is False


def test_one_failing_reader_does_not_stop_another():
    from jarvis.cost import balances

    saved = dict(balances._readers)
    try:
        balances._readers.clear()
        balances.register_reader("broken", lambda: (_ for _ in ()).throw(RuntimeError("401")))
        balances.register_reader("fine", lambda: {"remaining": 1.0})
        balances.register_reader("absent", lambda: None)
        result = balances.refresh_all()
        assert result["read"] == ["fine"]
        assert "broken" in result["failed"]
        assert store.get_balance("absent") is None
    finally:
        balances._readers.clear()
        balances._readers.update(saved)


# --- the tool -----------------------------------------------------------------

def test_check_spending_says_plainly_when_part_of_the_usage_has_no_price():
    from jarvis.tools.check_spending import SPEC

    store.record_event(provider="openai", model_id="priced", unit_kind="tokens", units_in=1000)
    store.record_event(provider="openai", model_id="unpriced", unit_kind="tokens", units_in=1000)
    prices.set_user_price(provider="openai", model_id="priced", price_in=0.001, price_out=0.0)

    answer = SPEC.handler(period="month")
    assert answer["ok"] and answer["calls"] == 2
    assert "unpriced" in answer["note"]
    assert answer["calculated"]["amount"] == pytest.approx(1.0)


def test_check_spending_on_an_empty_period_says_so_rather_than_reporting_zero_spend():
    from jarvis.tools.check_spending import SPEC

    answer = SPEC.handler(period="today")
    assert answer["measured"] == [] and answer["calculated"] is None
    assert "yet" in answer["note"]
