"""The driver conformance suite: the same cases, run against every driver through
the whole layer and a real socket (`stub_wire.StubWire`).

text · streaming event order · tool round trip (parallel calls, missing ids) ·
structured output · image input · usage mapping · every canonical error type
(including mid-stream and inside a 200) · sealed reasoning round trip · output
items fed back unchanged · discovery · embeddings.

What differs between formats is only how each spells things on the wire, read
back by the small per-format functions at the top — never what the layer promises.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from jarvis import config as secrets
from jarvis import models
from jarvis.models import errors, execute, state
from jarvis.models.types import (Done, ErrorEvent, ImagePart, Message, OutputSpec, Sealed, TextDelta, TextPart,
                                 Tool, ToolArgsDelta, ToolCall, ToolCallCompleted, ToolCallStarted, ToolResult)

from layer_helpers import ask, configure, layer  # noqa: F401
from stub_wire import StubWire, Turn

BUILT = ["openai_chat", "openai_responses", "anthropic_messages", "gemini_generate"]

#: What each format can do on the wire at all.
MISSING_IDS = {"openai_chat", "gemini_generate"}           # the server may give no tool-call ids
SEALED_KIND = {"openai_responses": "reasoning", "anthropic_messages": "thinking",
               "gemini_generate": "thought_signature"}   # provider reasoning state it round-trips

TOOLS = (Tool("get_weather", "Weather in a city.",
              {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}),
         Tool("get_time", "Time in a city.",
              {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}))


# --- reading each format's wire ------------------------------------------------------------------

def wire_call_ids(fmt: str, body: dict[str, Any]) -> list[str]:
    if fmt == "openai_chat":
        return [c["id"] for m in body["messages"] if m["role"] == "assistant" for c in m.get("tool_calls") or []]
    if fmt == "openai_responses":
        return [i["call_id"] for i in body["input"] if i.get("type") == "function_call"]
    if fmt == "anthropic_messages":
        return [b["id"] for m in body["messages"] if m["role"] == "assistant"
                for b in m["content"] if b.get("type") == "tool_use"]
    return [p["functionCall"].get("id") for c in body["contents"] if c["role"] == "model"
            for p in c["parts"] if "functionCall" in p]


def wire_result_ids(fmt: str, body: dict[str, Any]) -> list[str]:
    if fmt == "openai_chat":
        return [m["tool_call_id"] for m in body["messages"] if m["role"] == "tool"]
    if fmt == "openai_responses":
        return [i["call_id"] for i in body["input"] if i.get("type") == "function_call_output"]
    if fmt == "anthropic_messages":
        return [b["tool_use_id"] for m in body["messages"] if m["role"] == "user" and isinstance(m["content"], list)
                for b in m["content"] if b.get("type") == "tool_result"]
    return [p["functionResponse"].get("id") for c in body["contents"] if c["role"] == "user"
            for p in c["parts"] if "functionResponse" in p]


def wire_has_image(fmt: str, body: dict[str, Any]) -> bool:
    text = json.dumps(body)
    return {"openai_chat": '"image_url"', "openai_responses": '"input_image"',
            "anthropic_messages": '"type": "image"', "gemini_generate": '"inlineData"'}[fmt] in text \
        and "AAAA" in text


def wire_schema(fmt: str, body: dict[str, Any]) -> Any:
    if fmt == "openai_chat":
        return body["response_format"]["json_schema"]["schema"]
    if fmt == "openai_responses":
        return body["text"]["format"]["schema"]
    if fmt == "anthropic_messages":
        return body["output_config"]["format"]["schema"]
    return body["generationConfig"]["responseJsonSchema"]


def wire_sealed_replayed(fmt: str, body: dict[str, Any]) -> bool:
    text = json.dumps(body)
    return {"openai_responses": "ENC-rs_1", "anthropic_messages": "SIG-abc123",
            "gemini_generate": "GEMSIG-"}[fmt] in text


# --- set-up ---------------------------------------------------------------------------------------

@pytest.fixture(params=BUILT)
def wire(request, layer, monkeypatch):
    monkeypatch.setattr(execute, "sleep", lambda s: None)
    stub = StubWire(request.param, key="sk-stub")
    stub.start()
    secrets.save_secret("stub_key", "sk-stub")
    caps = {"text_in": True, "tools": True, "parallel_tools": True, "image_in": True,
            "structured_output_strict": True, "reasoning_control": True, "prompt_caching": True}
    configure(layer, [{"name": "stub", "driver": request.param, "base_url": stub.base_url, "trust": "standard",
                       "secret_ref": "stub_key", "models": {"stub-a": {"capabilities": caps}}}],
              settings={"retries": 0})
    yield stub
    stub.stop()


def fmt(stub: StubWire) -> str:
    return stub.fmt


def with_items(request, items):
    return type(request)(**{**request.__dict__, "items": tuple(items)})


# --- the cases -----------------------------------------------------------------------------------

def test_text(wire):
    wire.queue(Turn(text="Hello there, person.", model="stub-a-2026"))
    response = models.generate(ask())
    assert response.text == "Hello there, person."
    assert response.stop_reason == "stop"
    assert response.provenance.endpoint_id == "stub/stub-a"
    assert response.provenance.reported_model == "stub-a-2026"
    assert "sk-stub" in json.dumps(wire.generations()[-1]["headers"])


def test_streaming_event_order(wire):
    wire.queue(Turn(text="one two three", reasoning=False))
    events = list(models.stream(ask()))
    assert [type(e) for e in events[:-1]] == [TextDelta] * 3 and isinstance(events[-1], Done)
    assert "".join(e.text for e in events[:-1]) == "one two three"

    wire.queue(Turn(tools=[("get_weather", {"city": "Paris"}), ("get_time", {"city": "Tokyo"})]))
    events = [e for e in models.stream(ask(tools=TOOLS)) if not isinstance(e, TextDelta)]
    for call_id in {e.id for e in events if isinstance(e, ToolCallStarted)}:
        mine = [type(e).__name__ for e in events
                if getattr(e, "id", None) == call_id or (isinstance(e, ToolCallCompleted) and e.call.id == call_id)]
        assert mine[0] == "ToolCallStarted" and mine[-1] == "ToolCallCompleted" and "ToolArgsDelta" in mine
    assert isinstance(events[-1], Done)


@pytest.mark.parametrize("server_ids", [True, False])
def test_tool_round_trip_with_parallel_calls(wire, server_ids):
    if not server_ids and fmt(wire) not in MISSING_IDS:
        pytest.skip("this format always carries its own ids")
    wire.queue(Turn(tools=[("get_weather", {"city": "Paris"}), ("get_time", {"city": "Tokyo"})], ids=server_ids),
               Turn(text="Sunny in Paris, noon in Tokyo."))
    first = models.generate(ask(tools=TOOLS))
    calls = first.tool_calls
    assert [c.name for c in calls] == ["get_weather", "get_time"]
    assert [dict(c.arguments) for c in calls] == [{"city": "Paris"}, {"city": "Tokyo"}]
    assert len({c.id for c in calls}) == 2 and all(c.id.startswith("call_") for c in calls)
    assert first.stop_reason == "tool_calls"

    results = [ToolResult(c.id, c.name, {"answer": c.name}) for c in calls]
    second = models.generate(with_items(ask(tools=TOOLS), (*ask().items, *first.items, *results)))
    assert second.text == "Sunny in Paris, noon in Tokyo."
    body = wire.last_body()
    sent_calls, sent_results = wire_call_ids(fmt(wire), body), wire_result_ids(fmt(wire), body)
    assert len(sent_calls) == 2 and sent_calls == sent_results  # every result names its own call
    if server_ids:
        assert all(not i.startswith("call_") for i in sent_calls)  # the server gets its own ids back


def test_structured_output(wire):
    schema = {"type": "object", "properties": {"answer": {"type": "integer"}}, "required": ["answer"],
              "additionalProperties": False}
    wire.queue(Turn(text='{"answer": 5}', reasoning=False))
    response = models.generate(ask("2+3?", output=OutputSpec("json", schema, "required")))
    assert response.data == {"answer": 5}
    assert response.report.features["structured_output"] == "native"
    sent = wire_schema(fmt(wire), wire.last_body())
    assert sent["properties"]["answer"]["type"] == "integer"


def test_image_input(wire):
    picture = with_items(ask(), [Message("user", (TextPart("What is this?"), ImagePart("image/png", "AAAA")))])
    wire.queue(Turn(text="A red square."))
    assert models.generate(picture).text == "A red square."
    assert wire_has_image(fmt(wire), wire.last_body())


def test_usage_mapping(wire):
    wire.queue(Turn(usage={"input": 120, "output": 30, "cached": 40, "reasoning": 10}))
    usage = models.generate(ask()).usage
    assert (usage.input, usage.output, usage.cached) == (120, 30, 40)
    if fmt(wire) != "anthropic_messages":  # it reports thinking inside output, not apart
        assert usage.reasoning == 10
    assert usage.raw


@pytest.mark.parametrize("status,message,expected", [
    (429, "slow down", errors.RateLimited),
    (503, "overloaded", errors.Unavailable),
    (504, "gateway timeout", errors.Timeout),
    (401, "bad key", errors.Auth),
    (402, "payment required", errors.Auth),
    (403, "you exceeded your current quota", errors.Auth),
    (400, "bad request", errors.InvalidRequest),
    (400, "This model's maximum context length is 8192 tokens", errors.ContextTooLong),
    (400, "rejected by our content policy", errors.ContentRefused),
])
def test_every_http_error_maps_to_a_canonical_type(wire, status, message, expected):
    wire.queue(Turn(status=status, message=message, retry_after="7" if status == 429 else None))
    with pytest.raises(expected) as caught:
        models.generate(ask())
    # Said plainly; the provider's own words are kept aside for the trace, not shown.
    assert message not in str(caught.value) and message in caught.value.detail["provider_words"]
    if status == 429:
        assert caught.value.retry_after == 7.0


def test_an_error_mid_stream_is_an_error_event_after_the_text(wire):
    wire.queue(Turn(text="Half a sentence and", mid_stream="the upstream fell over", reasoning=False))
    events = list(models.stream(ask()))
    assert isinstance(events[0], TextDelta) and isinstance(events[-1], ErrorEvent)
    assert events[-1].error.retryable and "fell over" in events[-1].error.detail["provider_words"]


def test_an_error_inside_a_200_is_an_error(wire, layer):
    if fmt(wire) == "openai_chat":
        configure(layer, [{"name": "stub", "driver": "openai_chat", "base_url": wire.base_url, "trust": "standard",
                           "secret_ref": "stub_key", "quirks": "gateway", "models": {"stub-a": {}}}],
                  settings={"retries": 0})
    wire.queue(Turn(error_in_200={"code": 429, "message": "free tier limit reached"}))
    with pytest.raises(errors.RateLimited) as caught:
        models.generate(ask())
    assert "free tier limit" in caught.value.detail["provider_words"]


def test_a_reply_that_just_stops_is_unavailable(wire):
    wire.queue(Turn(text="and then", truncate=True, reasoning=False))
    with pytest.raises(errors.Unavailable, match="stopped part-way"):
        models.generate(ask())


def test_a_refusal_or_length_stop_is_reported_not_raised(wire):
    wire.queue(Turn(finish="length"))
    assert models.generate(ask()).stop_reason == "length"


def test_sealed_reasoning_round_trips_to_its_own_endpoint(wire):
    kind = SEALED_KIND.get(fmt(wire))
    if kind is None:
        pytest.skip("this server sends no reasoning state")
    wire.queue(Turn(tools=[("get_weather", {"city": "Paris"})]), Turn(text="Sunny."))
    first = models.generate(ask(tools=TOOLS))
    sealed = [i for i in first.items if isinstance(i, Sealed) and i.kind == kind]
    assert sealed and all(i.endpoint_id == "stub/stub-a" for i in sealed)
    call = first.tool_calls[0]
    models.generate(with_items(ask(tools=TOOLS), (*ask().items, *first.items,
                                                  ToolResult(call.id, call.name, {"t": 20}))))
    assert wire_sealed_replayed(fmt(wire), wire.last_body())


def test_output_items_fed_back_unchanged(wire):
    wire.queue(Turn(text="First answer."), Turn(text="Second answer."))
    first = models.generate(ask("one"))
    history = (*ask("one").items, *first.items, Message("user", (TextPart("two"),)))
    second = models.generate(with_items(ask(), history))
    assert second.text == "Second answer."
    assert "First answer." in json.dumps(wire.last_body())
    assert "one" in json.dumps(wire.last_body()) and "two" in json.dumps(wire.last_body())


def test_discovery_lists_models_and_an_unreachable_server_rests(wire, layer):
    result = models.refresh_catalog("stub")
    assert result["stub"]["ok"] and result["stub"]["models"] == 2
    assert {"stub/stub-a", "stub/stub-b"} <= {e.id for e in models.list_endpoints()}
    wire.stop()
    result = models.refresh_catalog("stub")
    assert not result["stub"]["ok"]
    assert "stub/stub-b" in {e.id for e in models.list_endpoints()}  # the last listing is kept
    assert state.connection_down("stub")
