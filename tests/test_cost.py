"""Cost tracking: measured and calculated, kept apart.

The load-bearing test here is the end-to-end one — a real adapter parsing a real
provider's usage chunk off a real socket, through the real gateway, into a real
row. Every layer of that path previously discarded the number, and a unit test
of the store alone would not have noticed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from jarvis import conversation
from jarvis.cost import prices, report, store
from jarvis.db import reset_for_tests as reset_db
from jarvis.events import EventType
from jarvis.events.bus import Event, EventBus
from jarvis.jscompat import to_iso_z
from jarvis.observers.cost import record_model_call
from jarvis.orchestrator.model_port import StepComplete


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    conversation.reset_for_tests()
    yield
    conversation.reset_for_tests()
    reset_db()


# --- reading what the provider said ------------------------------------------


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


# --- prices: never a guess ----------------------------------------------------

def test_an_unpriced_model_has_no_money_figure_at_all(scratch):
    _prices(scratch, {})
    assert prices.calculate(provider="openai", model_id="unpriced",
                            units_in=1000, units_out=1000) is None


# --- prices come from the model layer ------------------------------------------

PROVIDER = "openai_chat"


def _prices(scratch, models: dict, *, trust: str = "standard"):
    """A connection whose ledger name is PROVIDER, with these per-million-token prices
    ({model: (in, out)}) written the way the person would in their model settings."""
    import yaml

    from jarvis.models import config as model_config
    from jarvis.models import state as model_state

    (scratch.data_dir / "models.yaml").write_text(yaml.safe_dump({"connections": [
        {"name": "gw", "driver": "openai_chat", "base_url": "http://gw.test/v1", "trust": trust,
         "models": {m: ({"pricing": {"input": a, "output": b}} if a is not None else {})
                    for m, (a, b) in models.items()}}]}), encoding="utf-8")
    model_config.forget()
    model_state.reset()


@pytest.fixture(autouse=True)
def _forget_models(scratch):
    yield
    from jarvis.models import config as model_config
    from jarvis.models import state as model_state

    model_config.forget()
    model_state.reset()


def test_a_price_on_the_endpoint_is_multiplied_by_the_real_count(scratch):
    _prices(scratch, {"m": (1000.0, 2000.0)})
    money = prices.calculate(provider=PROVIDER, model_id="m", units_in=1000, units_out=500)
    assert money["amount"] == pytest.approx(1.0 + 1.0)
    assert money["priceSource"] == "user"


def test_a_model_the_layer_has_no_price_for_is_unpriced(scratch):
    _prices(scratch, {"m": (None, None)})
    assert prices.calculate(provider=PROVIDER, model_id="m", units_in=1000) is None


def test_a_price_a_server_listed_is_used_and_said_so(scratch):
    from jarvis.models import state as model_state
    from jarvis.models.catalog import Pricing
    from jarvis.models.prepared import Discovered

    _prices(scratch, {})
    model_state.record_discovery("gw", [Discovered("vendor/model-x", pricing=Pricing(1.5, 6.0)),
                                        Discovered("vendor/unpriced")])
    money = prices.calculate(provider=PROVIDER, model_id="vendor/model-x", units_in=1_000_000)
    assert money["amount"] == pytest.approx(1.5) and money["priceSource"] == "provider_reported"
    assert prices.calculate(provider=PROVIDER, model_id="vendor/unpriced", units_in=1000) is None


def test_a_connection_on_the_persons_own_machine_is_free_not_unpriced(scratch):
    _prices(scratch, {"llama3": (None, None)}, trust="local")
    money = prices.calculate(provider=PROVIDER, model_id="llama3", units_in=5000, units_out=5000)
    assert money["amount"] == 0.0 and money["free"] is True


def test_only_token_usage_has_a_price(scratch):
    _prices(scratch, {"m": (1.0, 1.0)})
    assert prices.calculate(provider=PROVIDER, model_id="m", unit_kind="characters", units_in=100) is None


# --- the report keeps its three kinds of number apart -------------------------

def test_unpriced_usage_is_counted_and_named_rather_than_costed_at_zero(scratch):
    _prices(scratch, {"priced": (1000.0, 0.0)})
    store.record_event(provider=PROVIDER, model_id="priced", unit_kind="tokens",
                       units_in=1000, units_out=0)
    store.record_event(provider=PROVIDER, model_id="unpriced", unit_kind="tokens",
                       units_in=1000, units_out=0)

    data = report.usage_breakdown("1970-01-01T00:00:00.000Z")
    assert data["calculated"]["amount"] == pytest.approx(1.0)
    assert [g["modelId"] for g in data["pricelessGroups"]] == ["unpriced"]
    assert {g["modelId"] for g in data["measured"]["groups"]} == {"priced", "unpriced"}


def test_nothing_priced_at_all_gives_no_money_figure_rather_than_zero(scratch):
    _prices(scratch, {})
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

# --- balances stay separate ---------------------------------------------------

# --- the tool -----------------------------------------------------------------

def test_check_spending_says_plainly_when_part_of_the_usage_has_no_price(scratch):
    from jarvis.tools.check_spending import SPEC

    _prices(scratch, {"priced": (1000.0, 0.0)})
    store.record_event(provider=PROVIDER, model_id="priced", unit_kind="tokens", units_in=1000)
    store.record_event(provider=PROVIDER, model_id="unpriced", unit_kind="tokens", units_in=1000)

    answer = SPEC.handler(period="month")
    assert answer["ok"] and answer["calls"] == 2
    assert "unpriced" in answer["note"]
    assert answer["calculated"]["amount"] == pytest.approx(1.0)


def test_check_spending_on_an_empty_period_says_so_rather_than_reporting_zero_spend():
    from jarvis.tools.check_spending import SPEC

    answer = SPEC.handler(period="today")
    assert answer["measured"] == [] and answer["calculated"] is None
    assert "yet" in answer["note"]


# --- free is a price; unpriced is not -----------------------------------------
#
# These two states both come out as "no money owed", and conflating them is the
# failure this section exists to prevent: reporting an unpriced model as $0
# understates spend, and reporting a free model as unpriced makes a genuinely
# free month look like a month with no data.

def test_free_usage_is_reported_as_free_and_unpriced_usage_as_unknown(scratch):
    _prices(scratch, {"llama3": (0.0, 0.0), "mystery": (None, None)})
    store.record_event(provider=PROVIDER, model_id="llama3",
                       unit_kind="tokens", units_in=5000, units_out=5000)
    store.record_event(provider=PROVIDER, model_id="mystery",
                       unit_kind="tokens", units_in=1000, units_out=0)

    data = report.usage_breakdown("1970-01-01T00:00:00.000Z")
    assert [g["modelId"] for g in data["freeGroups"]] == ["llama3"]
    assert [g["modelId"] for g in data["pricelessGroups"]] == ["mystery"]
    # The free one is IN the total (at zero); the unpriced one is not in it at all.
    assert data["calculated"] == {"amount": 0.0, "currency": "USD"}


def test_a_month_of_entirely_free_usage_is_a_real_zero_not_a_missing_total(scratch):
    _prices(scratch, {"llama3": (0.0, 0.0)})
    store.record_event(provider=PROVIDER, model_id="llama3",
                       unit_kind="tokens", units_in=9999, units_out=9999)
    data = report.usage_breakdown("1970-01-01T00:00:00.000Z")
    assert data["calculated"] == {"amount": 0.0, "currency": "USD"}
    assert data["pricelessGroups"] == []


def test_check_spending_says_free_rather_than_unknown(scratch):
    from jarvis.tools.check_spending import SPEC

    _prices(scratch, {"llama3": (0.0, 0.0)})
    store.record_event(provider=PROVIDER, model_id="llama3",
                       unit_kind="tokens", units_in=100, units_out=100)
    answer = SPEC.handler(period="month")
    assert "cost nothing" in answer["note"] and "free" in answer["note"]
    assert answer["calculated"]["amount"] == 0.0
    assert [g["modelId"] for g in answer["freeGroups"]] == ["llama3"]


