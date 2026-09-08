"""The model gateway (§26, §27), exercised over real HTTP against a stub server.

The adapter under test is the REAL adapter — the stub speaks the wire format, so
SSE framing, the empty-`choices` usage chunk and tool-call deltas are all
genuinely parsed rather than mocked past.

Weighted toward the four §26/§27 defects the audit found: a probe that proved
listing rather than generation; a name regex that matched "mini" inside "gemini";
an adapter ceiling used as a hard gate; and a failure path that benched a model
in one caller and not in the others.
"""

from __future__ import annotations

import pytest

from jarvis.adapters import get_capabilities
from jarvis.gateway import availability, connections, probe, registry, routing
from jarvis.gateway.catalog import catalog_defaults, with_capability_defaults
from jarvis.gateway.client import Gateway, NoModelAvailable
from jarvis.gateway.error_kind import classify_error
from jarvis.gateway.routing import Task, build_candidates, explain_exclusions
from jarvis.orchestrator.model_port import ModelSwitched, StepComplete, TextChunk
from jarvis.events.bus import EventBus

from stub_openai_server import StubModelServer


@pytest.fixture(autouse=True)
def _isolate(scratch):
    availability.reset_for_tests()
    yield
    availability.reset_for_tests()


@pytest.fixture
def stub():
    server = StubModelServer()
    server.base_url = server.start()
    yield server
    server.stop()


def connect(stub, *, label="stub", secret=None, models=("stub-model",), key_required=False):
    conn = connections.add_connection(
        adapter="openai-compatible", base_url=stub.base_url, label=label,
        secret=secret, provider="custom", kind="local", key_required=key_required)
    return [registry.add_model(connection_id=conn["id"], model=m) for m in models]


# --- connections and models --------------------------------------------------

def test_a_model_inherits_its_connection_s_facts_at_read_time(stub):
    [model] = connect(stub)
    fresh = registry.get_model(model["id"])
    assert fresh["baseUrl"] == stub.base_url
    assert fresh["adapter"] == "openai-compatible"
    assert fresh["kind"] == "local"
    # Capability flags a saved model predates appear without a migration.
    assert set(fresh["caps"]) >= {"vision", "video", "audio", "webSearch"}


def test_a_model_cannot_be_repointed_at_another_connection_by_patching_it(stub):
    [model] = connect(stub)
    patched = registry.update_model(model["id"], {"baseUrl": "http://evil", "label": "renamed"})
    assert patched["label"] == "renamed"
    assert patched["baseUrl"] == stub.base_url, "adapter/address must come from the connection"


def test_the_same_model_name_may_exist_under_two_connections(stub):
    a = connections.add_connection(adapter="openai-compatible", base_url=stub.base_url, label="one")
    b = connections.add_connection(adapter="openai-compatible", base_url=stub.base_url, label="two")
    registry.add_model(connection_id=a["id"], model="stub-model")
    registry.add_model(connection_id=b["id"], model="stub-model")
    assert len(registry.list_models()) == 2
    with pytest.raises(ValueError):
        registry.add_model(connection_id=a["id"], model="stub-model")


def test_deleting_a_connection_takes_its_models_with_it(stub):
    [model] = connect(stub)
    removed = registry.delete_connection(registry.get_model(model["id"])["connectionId"])
    assert removed == 1 and registry.list_models() == []


# --- the catalog fixes -------------------------------------------------------

def test_mini_inside_gemini_no_longer_scores_a_pro_model_as_fast():
    """The real defect: an unbounded `mini` matched every Gemini model."""
    pro = catalog_defaults("gemini", "gemini-4-pro-preview")
    assert pro["tier"]["quality"] == 5 and pro["tier"]["speed"] == 2
    genuinely_small = catalog_defaults("openai-compatible", "gpt-4o-mini")
    assert genuinely_small["tier"]["speed"] == 5


def test_a_guessed_entry_says_it_is_a_guess():
    assert catalog_defaults("gemini", "something-nobody-knows").get("guessed") is True
    assert "guessed" not in catalog_defaults("gemini", "gemini-3-pro")


def test_an_adapter_ceiling_seeds_capabilities_and_the_user_can_override():
    """§26: the ceiling must not be a permanent gate — that is what made video
    and web search Gemini-only regardless of what the user knew."""
    assert get_capabilities("openai-compatible")["video"] is False
    seeded = with_capability_defaults(None, "openai-compatible", "some-model")
    assert seeded["video"] is False
    overridden = with_capability_defaults({"video": True}, "openai-compatible", "some-model")
    assert overridden["video"] is True, "an explicit user value must survive"


def test_a_gateway_hosted_model_is_not_assumed_to_see_images():
    caps = with_capability_defaults(None, "openai-compatible", "some/music-model",
                                    "https://openrouter.ai/api/v1", "gateway")
    assert caps["vision"] is False
    named = with_capability_defaults(None, "openai-compatible", "some/llava-vl",
                                     "https://openrouter.ai/api/v1", "gateway")
    assert named["vision"] is True


# --- routing -----------------------------------------------------------------

def entry(model_id, **kw):
    base = {"id": model_id, "model": model_id, "enabled": True, "keyRequired": False,
            "caps": {"tools": True}, "tier": {"speed": 3, "quality": 3, "cost": 2}}
    base.update(kw)
    return base


def test_a_known_bad_model_never_outranks_one_that_works():
    good = entry("good", tier={"speed": 1, "quality": 1, "cost": 4})
    bad = entry("bad", tier={"speed": 5, "quality": 5, "cost": 0})
    availability.record("bad", "quota", detail="out of quota")
    ranked = build_candidates(Task(text="hello"), entries=[bad, good])
    assert [e["id"] for e in ranked] == ["good"], "a benched model must not be offered"


def test_ties_break_deterministically_not_on_file_order():
    a = entry("zeta", tier={"speed": 5, "quality": 3, "cost": 1})
    b = entry("alpha", tier={"speed": 5, "quality": 3, "cost": 1})
    assert [e["id"] for e in build_candidates(Task(), entries=[a, b])] == ["alpha", "zeta"]


def test_a_need_is_enforced_by_the_router_itself():
    """In the Node version the router had no concept of this, so the check lived
    in one caller and was missing from another entirely."""
    blind = entry("blind", caps={"tools": True, "vision": False})
    seeing = entry("seeing", caps={"tools": True, "vision": True})
    task = Task(text="what's in this picture", need={"vision": True})
    assert [e["id"] for e in build_candidates(task, entries=[blind, seeing])] == ["seeing"]
    assert explain_exclusions(task, entries=[blind, seeing])["counts"] == {"no_vision": 1}


def test_a_pin_leads_but_does_not_become_a_single_point_of_failure():
    a, b = entry("a"), entry("b")
    ranked = build_candidates(Task(), model_id="b", entries=[a, b])
    assert [e["id"] for e in ranked] == ["b", "a"]


def test_exclusions_are_explained_with_the_same_predicate_that_excluded():
    off = entry("off", enabled=False)
    toolless = entry("toolless", caps={"tools": False})
    counts = explain_exclusions(Task(), entries=[off, toolless])["counts"]
    assert counts == {"disabled": 1, "no_tools": 1}


# --- error classification ----------------------------------------------------

@pytest.mark.parametrize("status,expected", [
    (429, "quota"), (401, "auth"), (403, "no_access"), (404, "unsupported"),
    (503, "transient"),
])
def test_status_codes_classify(status, expected):
    err = RuntimeError("upstream")
    err.status_code = status
    assert classify_error(err) == expected


def test_a_context_length_error_never_benches_the_model_permanently():
    """A 400 about context is a per-TURN problem; treating it as 'unsupported'
    would exclude the model from every future turn."""
    assert classify_error(RuntimeError("Invalid request: maximum context length exceeded")) == "other"
    assert classify_error(RuntimeError("Request contains an invalid argument.")) == "unsupported"


# --- the gateway, end to end -------------------------------------------------

def collect(gateway, **kw):
    return list(gateway.stream(messages=[{"role": "user", "text": "hello"}],
                               system="be brief", tools=[], session_id="s1", **kw))


def test_a_real_streamed_reply_arrives_in_pieces_and_completes_once(stub):
    stub.says("All good here.")
    connect(stub)
    events = collect(Gateway(event_bus=EventBus()))
    chunks = [e for e in events if isinstance(e, TextChunk)]
    completed = [e for e in events if isinstance(e, StepComplete)]
    assert len(chunks) > 1, "the stub chunks deliberately awkwardly"
    assert len(completed) == 1
    assert "".join(c.text for c in chunks) == "All good here."
    assert completed[0].text == "All good here."


def test_a_tool_call_survives_being_streamed_as_deltas(stub):
    stub.calls_tool("get_weather", {"where": "here"})
    connect(stub)
    [completed] = [e for e in collect(Gateway(event_bus=EventBus())) if isinstance(e, StepComplete)]
    assert len(completed.tool_calls) == 1
    assert completed.tool_calls[0].name == "get_weather"
    assert completed.tool_calls[0].args == {"where": "here"}


def test_a_failure_benches_the_model_and_the_next_one_takes_over(stub):
    stub.fails(429, "Rate limit exceeded").says("Second model here.")
    first, second = connect(stub, models=("model-a", "model-b"))
    events = collect(Gateway(event_bus=EventBus()))

    text = "".join(e.text for e in events if isinstance(e, TextChunk))
    assert text == "Second model here."
    benched = availability.status_of(first["id"]) or availability.status_of(second["id"])
    assert benched and benched["state"] == "quota"


def test_a_switch_is_announced_rather_than_made_silently(stub):
    """A turn that quietly took three attempts must not look like one that took
    none — and a reply that changes course needs to say so."""
    stub.fails(429, "Rate limit exceeded").says("Recovered.")
    connect(stub, models=("model-a", "model-b"))
    events = collect(Gateway(event_bus=EventBus()))
    switches = [e for e in events if isinstance(e, ModelSwitched)]
    assert len(switches) == 1
    assert switches[0].from_model and switches[0].to_model != switches[0].from_model
    assert "Rate limit" in switches[0].reason


def test_when_nothing_can_answer_the_error_names_the_real_reasons(stub):
    stub.fails(429, "Rate limit exceeded")
    connect(stub, models=("only-model",))
    with pytest.raises(NoModelAvailable) as raised:
        collect(Gateway(event_bus=EventBus()))
    assert "Rate limit exceeded" in str(raised.value)

    # And on the NEXT turn it is excluded up front, with a real retry estimate.
    with pytest.raises(NoModelAvailable) as again:
        collect(Gateway(event_bus=EventBus()))
    assert "out of quota" in str(again.value)
    assert again.value.detail["soonestRetryMs"] > 0


def test_every_call_reports_itself_on_the_event_bus(stub):
    stub.fails(429, "nope").says("fine")
    connect(stub, models=("a", "b"))
    seen = []
    ebus = EventBus()
    ebus.subscribe(None, lambda e: seen.append(e.type.value))
    collect(Gateway(event_bus=ebus))
    assert "model.call_failed" in seen and "model.call_completed" in seen


# --- the probe ---------------------------------------------------------------

def test_a_probe_proves_generation_not_just_listing(stub):
    """The §26 fix. A key that can LIST is not a key that can GENERATE — the
    Node probe concluded from a successful listing that a keyless connection
    worked, and it then 401'd on the user's first real question."""
    stub.script = [{"status": 401, "message": "Unauthorized"}]
    # The listing itself is fine; the generation attempt is what fails.
    stub.script = []
    stub.says("ready")
    result = probe.probe_endpoint(stub.base_url)
    assert result.ok and result.adapter == "openai-compatible"
    assert result.key_required is False and result.kind == "local"
    assert any("prove it can actually answer" in step for step in result.steps)


def test_a_connection_that_lists_but_cannot_generate_is_not_saved_as_working(stub):
    stub.script = [{"status": 401, "message": "Unauthorized"}]
    result = probe.probe_endpoint(stub.base_url)
    assert result.ok is False
    assert result.needs_key is True
    assert "needs an API key" in (result.error or "")
    assert "that key is invalid" not in (result.error or "").lower()


def test_a_probe_shows_its_working_even_when_it_fails():
    result = probe.probe_endpoint("http://127.0.0.1:19999")
    assert result.ok is False
    assert len(result.steps) >= 2, "every attempt must be narrated, not swallowed"
