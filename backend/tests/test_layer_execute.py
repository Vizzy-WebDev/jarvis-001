"""The executor, with the fake driver: retries, fallback and its rules, the streaming
rule, the breaker, rate-limit rests, limits, spend — and that explain_route
predicts exactly what the call does."""

from __future__ import annotations

import threading
import time

import pytest

from jarvis import models
from jarvis.models import errors, execute, state
from jarvis.models.drivers import fake
from jarvis.models.types import (Done, ErrorEvent, Message, OutputSpec, Requirements, Sealed, TextDelta, TextPart,
                                 Tool, ToolCall, ToolCallCompleted, ToolCallStarted, ToolResult, Usage)

from layer_helpers import CHAT, ask, configure, fake_conn, layer  # noqa: F401


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    waits: list[float] = []
    monkeypatch.setattr(execute, "sleep", waits.append)
    return waits


def attempted(response_or_error) -> list[tuple[str, str | None]]:
    attempts = response_or_error.provenance.attempts
    return [(a.endpoint_id, a.error_type) for a in attempts]


def test_a_plain_answer_carries_provenance_usage_and_cost(layer):
    configure(layer, [fake_conn("a", models={"m": {"pricing": {"input": 1, "output": 2}}})])
    fake.queue("a", fake.reply("hi there", usage=Usage(input=1000, output=500), model="m-2026"))
    response = models.generate(ask())
    assert response.text == "hi there" and response.stop_reason == "stop"
    assert response.provenance.endpoint_id == "a/m" and response.provenance.reported_model == "m-2026"
    assert response.usage.cost == pytest.approx((1000 * 1 + 500 * 2) / 1_000_000)
    assert state.month_spend() == pytest.approx(response.usage.cost)
    assert response.request_id


def test_a_retryable_error_is_retried_on_the_same_endpoint_first(layer, no_waiting):
    configure(layer, [fake_conn("a"), fake_conn("b")])
    fake.queue("a", errors.Unavailable("busy"), fake.reply("second try"))
    response = models.generate(ask())
    assert response.text == "second try"
    assert attempted(response) == [("a/m", "unavailable"), ("a/m", None)]
    assert no_waiting == [1.0]


def test_after_retries_it_falls_back_preferring_a_different_upstream(layer):
    configure(layer, [fake_conn("gw", models={"x": {"upstream": "vendor-1"}, "y": {"upstream": "vendor-1"},
                                              "z": {"upstream": "vendor-2"}})])
    fake.queue("gw", *[errors.Unavailable("vendor-1 down")] * 3, fake.reply("from vendor 2"))
    response = models.generate(ask())
    assert response.provenance.endpoint_id == "gw/z"  # not gw/y: same upstream as the one that failed
    assert [f.to_endpoint for f in response.provenance.fallbacks] == ["gw/z"]
    assert "vendor-1 down" in response.provenance.fallbacks[0].reason


@pytest.mark.parametrize("error", [errors.ContextTooLong("too long"), errors.InvalidRequest("bad"),
                                   errors.ContentRefused("no")])
def test_a_non_retryable_error_ends_the_call(layer, error):
    configure(layer, [fake_conn("a"), fake_conn("b")])
    fake.queue("a", error)
    with pytest.raises(type(error)):
        models.generate(ask())
    assert [c.connection for c in fake.calls()] == ["a"]


@pytest.mark.parametrize("error", [errors.Auth("key refused"), errors.Auth("quota exhausted (billing)")])
def test_an_auth_refusal_ends_a_pinned_call(layer, error):
    """Under Auto it falls back instead — the approved deviation (D3b), pinned in
    test_layer_review_fixes.py."""
    configure(layer, [fake_conn("a"), fake_conn("b")], aliases={"pick": {"endpoint": "a/m"}})
    fake.queue("a", error)
    with pytest.raises(type(error)):
        models.generate(ask(requirements=Requirements(pin="pick")))
    assert [c.connection for c in fake.calls()] == ["a"]


def test_the_list_running_out_returns_the_last_error(layer):
    configure(layer, [fake_conn("a"), fake_conn("b")], settings={"retries": 0})
    fake.queue("a", errors.Unavailable("a down"))
    fake.queue("b", errors.Timeout("b slow"))
    with pytest.raises(errors.Timeout, match="b slow"):
        models.generate(ask())


def test_sensitive_data_never_reaches_a_disallowed_trust_class_even_on_fallback(layer):
    configure(layer, [fake_conn("home", trust="local"), fake_conn("cloud", trust="standard"),
                      fake_conn("home2", trust="local")],
              settings={"retries": 0}, policies={"data_classes": {"sensitive": ["local"]}})
    fake.queue("home", errors.Unavailable("down"))
    fake.queue("home2", errors.Unavailable("down"))
    with pytest.raises(errors.Unavailable):
        models.generate(ask(data_class="sensitive"))
    assert "cloud" not in {c.connection for c in fake.calls()}
    # The same failures with personal data do reach the cloud: nothing else was restricted.
    fake.reset()
    fake.queue("home", errors.Unavailable("down"))
    assert models.generate(ask(data_class="personal")).provenance.endpoint_id == "cloud/m"


def test_a_fallback_that_changes_family_is_skipped_when_not_allowed(layer):
    configure(layer, [fake_conn("a", models={"m": {"family": "one"}}), fake_conn("b", models={"m": {"family": "two"}}),
                      fake_conn("c", models={"m": {"family": "one"}})], settings={"retries": 0})
    fake.queue("a", errors.Unavailable("down"))
    response = models.generate(ask(requirements=Requirements(allow_family_change=False)))
    assert response.provenance.endpoint_id == "c/m"
    assert "b" not in {c.connection for c in fake.calls()}
    assert any("different model family" in w for w in response.report.warnings)


def test_a_required_schema_never_goes_to_an_emulating_endpoint(layer):
    configure(layer, [fake_conn("loose", models={"m": {"capabilities": {"json_mode": True}}}),
                      fake_conn("strict", models={"m": {"capabilities": {"structured_output_strict": True}}})])
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    fake.queue("strict", fake.reply('{"ok": true}'))
    response = models.generate(ask(output=OutputSpec("json", schema, "required")))
    assert response.data == {"ok": True} and response.report.features["structured_output"] == "native"
    assert {c.connection for c in fake.calls()} == {"strict"}


def test_stream_events_arrive_in_order_and_end_with_done(layer):
    configure(layer, [fake_conn("a")])
    fake.queue("a", fake.tools(("look", '{"q": "x"}'), text="Let me look."))
    events = list(models.stream(ask(tools=(Tool("look", "", {"type": "object"}),))))
    kinds = [type(e).__name__ for e in events]
    assert kinds == ["TextDelta", "ToolCallStarted", "ToolArgsDelta", "ToolCallCompleted", "Done"]
    done = events[-1].response
    assert [type(i).__name__ for i in done.items] == ["Message", "ToolCall"]
    assert done.tool_calls[0].id == events[1].id and done.tool_calls[0].arguments == {"q": "x"}


def test_a_stream_falls_back_before_its_first_event(layer):
    configure(layer, [fake_conn("a"), fake_conn("b")], settings={"retries": 0})
    fake.queue("a", errors.Unavailable("down"))
    fake.queue("b", fake.reply("from b"))
    events = list(models.stream(ask()))
    assert isinstance(events[-1], Done) and events[-1].response.provenance.endpoint_id == "b/m"


def test_no_fallback_after_the_first_streamed_event(layer):
    configure(layer, [fake_conn("a"), fake_conn("b")])
    fake.queue("a", fake.broken_after("Half a sen", errors.Unavailable("connection dropped")))
    events = list(models.stream(ask()))
    assert [type(e) for e in events] == [TextDelta, ErrorEvent]
    assert events[1].error.type == "unavailable"
    assert {c.connection for c in fake.calls()} == {"a"}  # no second attempt anywhere


def test_generate_may_fall_back_even_after_partial_output(layer):
    """Nothing reached the caller yet, so the rule about the first event doesn't apply."""
    configure(layer, [fake_conn("a"), fake_conn("b")], settings={"retries": 0})
    fake.queue("a", fake.broken_after("Half", errors.Unavailable("dropped")))
    assert models.generate(ask()).provenance.endpoint_id == "b/m"


def test_affinity_sticks_and_moves_after_a_fallback(layer):
    configure(layer, [fake_conn("a"), fake_conn("b")], settings={"retries": 0})
    fake.queue("b", fake.reply("b"))
    models.generate(ask(prefer=(), affinity_key="s1", requirements=Requirements()))  # lands on a
    assert models.explain_route(ask(affinity_key="s1")).ranked[0] == "a/m"
    fake.queue("a", errors.Unavailable("down"))
    assert models.generate(ask(affinity_key="s1")).provenance.endpoint_id == "b/m"
    assert models.explain_route(ask(affinity_key="s1")).ranked[0] == "b/m"


def test_explain_route_predicts_the_order_the_call_tries(layer):
    configure(layer, [fake_conn("a", models={"m": {"pricing": {"input": 3, "output": 3}}}),
                      fake_conn("b", models={"m": {"pricing": {"input": 1, "output": 1}}}),
                      fake_conn("c", models={"m": {"pricing": {"input": 2, "output": 2}}}),
                      fake_conn("d", trust="standard")],
              settings={"retries": 0}, policies={"data_classes": {"sensitive": ["local"]}})
    request = ask(data_class="sensitive", optimize="cost")
    explanation = models.explain_route(request)
    for name in "abc":
        fake.queue(name, errors.Unavailable(f"{name} down"))
    with pytest.raises(errors.Unavailable):
        models.generate(request)
    assert [f"{c.connection}/m" for c in fake.calls()] == list(explanation.ranked) == ["b/m", "c/m", "a/m"]
    assert {r.endpoint_id: r.reason for r in explanation.rejected} == {"d/m": "trust_not_allowed"}


def test_the_breaker_opens_after_repeated_failures_and_recovers(layer, monkeypatch):
    clock = [1_000.0]
    monkeypatch.setattr(state, "now", lambda: clock[0])
    configure(layer, [fake_conn("a"), fake_conn("b")], settings={"retries": 0, "breaker_threshold": 2,
                                                                 "breaker_base_s": 60})
    for _ in range(2):
        fake.queue("a", errors.Unavailable("down"))
        models.generate(ask())
    assert {r.endpoint_id: r.reason for r in models.explain_route(ask()).rejected} == {"a/m": "resting"}
    clock[0] += 61
    assert models.explain_route(ask()).ranked == ("a/m", "b/m")
    models.generate(ask())
    assert state.health("a/m")["streak"] == 0


def test_a_429_rests_the_connection_and_a_long_wait_moves_on_at_once(layer, no_waiting):
    configure(layer, [fake_conn("a"), fake_conn("b")])
    fake.queue("a", errors.RateLimited("slow down", retry_after=45))
    response = models.generate(ask())
    assert response.provenance.endpoint_id == "b/m"
    assert no_waiting == []  # the server asked for 45s: not waited for, moved on
    assert {r.endpoint_id: r.reason for r in models.explain_route(ask()).rejected} == {"a/m": "rate_limited"}


def test_a_short_retry_after_is_honoured(layer, no_waiting):
    configure(layer, [fake_conn("a")])
    fake.queue("a", errors.RateLimited("slow down", retry_after=2), fake.reply("ok"))
    assert models.generate(ask()).text == "ok"
    assert no_waiting == [2]


def test_concurrency_limits_hold(layer, monkeypatch):
    monkeypatch.setattr(execute, "sleep", time.sleep)
    configure(layer, [fake_conn("a", limits={"concurrency": 1})])
    inside, peak = [0], [0]
    lock = threading.Lock()

    def slow(prepared):
        with lock:
            inside[0] += 1
            peak[0] = max(peak[0], inside[0])
        time.sleep(0.05)
        with lock:
            inside[0] -= 1
        return fake.reply("ok")

    for _ in range(4):
        fake.queue("a", slow)
    threads = [threading.Thread(target=models.generate, args=(ask(),)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 1


def test_rpm_limits_wait_for_a_slot(layer, monkeypatch, no_waiting):
    now = [0.0]
    monkeypatch.setattr(execute, "clock", lambda: now[0])

    def advance(seconds):
        no_waiting.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(execute, "sleep", advance)
    configure(layer, [fake_conn("a", limits={"rpm": 2})])
    for _ in range(3):
        models.generate(ask())
    assert no_waiting and no_waiting[0] == pytest.approx(60.0)


def test_invalid_tool_arguments_come_back_raw_with_a_warning(layer):
    configure(layer, [fake_conn("a")])
    fake.queue("a", fake.tools(("look", '{"q": "unterminated')))
    response = models.generate(ask(tools=(Tool("look", "", {"type": "object"}),)))
    call = response.tool_calls[0]
    assert call.arguments is None and call.raw_arguments == '{"q": "unterminated'
    assert any("weren't valid JSON" in w for w in response.report.warnings)


def test_an_empty_reply_is_returned_as_it_is(layer):
    configure(layer, [fake_conn("a"), fake_conn("b")])
    fake.queue("a", fake.reply(""))
    response = models.generate(ask())
    assert response.items == () and response.provenance.endpoint_id == "a/m"


def test_foreign_provider_state_is_dropped_and_reported_and_our_own_is_kept(layer):
    configure(layer, [fake_conn("a")])
    history = (Message("user", (TextPart("hi"),)),
               Sealed("elsewhere/m", "thinking", {"sig": "x"}),
               Sealed("a/m", "thinking", {"sig": "mine"}),
               Message("assistant", (TextPart("hello"),)),
               Message("user", (TextPart("again"),)))
    request = ask()
    request = type(request)(**{**request.__dict__, "items": history})
    response = models.generate(request)
    sent = fake.calls("a")[0].prepared.items
    assert Sealed("a/m", "thinking", {"sig": "mine"}) in sent
    assert not any(isinstance(i, Sealed) and i.endpoint_id == "elsewhere/m" for i in sent)
    assert response.report.features["foreign_provider_state"] == "dropped"
    assert response.report.warnings


def test_output_items_go_back_in_unchanged_and_ids_stay_canonical(layer):
    configure(layer, [fake_conn("a"), fake_conn("b")])
    tool = (Tool("look", "", {"type": "object"}),)
    fake.queue("a", fake.tools(("look", '{"q": 1}'), native_ids=True, sealed=[("thinking", {"sig": "s1"})]))
    first = models.generate(ask(tools=tool))
    call = first.tool_calls[0]
    assert call.id.startswith("call_")
    ids_item = [i for i in first.items if isinstance(i, Sealed) and i.kind == "ids"][0]
    assert ids_item.payload == {call.id: "native_0"} and ids_item.endpoint_id == "a/m"

    follow_up = ask(tools=tool)
    follow_up = type(follow_up)(**{**follow_up.__dict__, "items": follow_up.items + first.items + (
        ToolResult(call.id, "look", {"answer": 42}),)})
    models.generate(follow_up)
    assert fake.calls("a")[1].prepared.items[1:1 + len(first.items)] == first.items  # unchanged

    # The same conversation on another endpoint: its own state is left out, the ids are the same.
    fake.queue("a", errors.Unavailable("down"), errors.Unavailable("down"), errors.Unavailable("down"))
    moved = models.generate(follow_up)
    assert moved.provenance.endpoint_id == "b/m"
    sent = fake.calls("b")[0].prepared.items
    assert [i.id for i in sent if isinstance(i, ToolCall)] == [call.id]
    assert [i.call_id for i in sent if isinstance(i, ToolResult)] == [call.id]
    assert not any(isinstance(i, Sealed) for i in sent)
    assert moved.report.features["foreign_provider_state"] == "dropped"


def test_nothing_eligible_fails_in_plain_language(layer):
    configure(layer, [fake_conn("cloud", trust="standard")], aliases={"selected": {"endpoint": "cloud/m"}},
              policies={"data_classes": {"sensitive": ["local"]}})
    with pytest.raises(errors.NoEligibleEndpoint, match="privacy settings"):
        models.generate(ask(data_class="sensitive", requirements=Requirements(pin="selected")))
    events = list(models.stream(ask(data_class="sensitive", requirements=Requirements(pin="selected"))))
    assert len(events) == 1 and isinstance(events[0], ErrorEvent)


def test_a_driver_that_cannot_read_its_reply_is_a_plain_failure(layer):
    configure(layer, [fake_conn("a")], settings={"retries": 0})

    def garbled(prepared):
        raise KeyError("choices")

    fake.queue("a", garbled)
    with pytest.raises(errors.Unavailable, match="couldn't read"):
        models.generate(ask())
