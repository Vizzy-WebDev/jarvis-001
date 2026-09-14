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

from jarvis.catalog import Capabilities, Effort, Lifecycle, Support
from jarvis.gateway import (
    availability, connections, deployments, effort as effort_store, latency,
    probe, routing, slots,
)
from jarvis.gateway.slots import Role
from jarvis.gateway.client import Gateway, NoModelAvailable
from jarvis.gateway.error_kind import classify_error
from jarvis.gateway.routing import Task, build_candidates, explain_exclusions
from jarvis.orchestrator.model_port import ModelSwitched, StepComplete, TextChunk
from jarvis.events import EventType
from jarvis.events.bus import EventBus

from conftest import candidate
from stub_openai_server import StubModelServer


@pytest.fixture(autouse=True)
def _isolate(scratch):
    for module in (availability, effort_store, latency, slots):
        module.reset_for_tests()
    yield
    for module in (availability, effort_store, latency, slots):
        module.reset_for_tests()


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
    return [deployments.add_deployment(connection_id=conn["id"], model=m) for m in models]


# --- connections and models --------------------------------------------------

def test_a_model_inherits_its_connection_s_facts_at_read_time(stub):
    [model] = connect(stub)
    fresh = deployments.get_deployment(model["id"])
    assert fresh["baseUrl"] == stub.base_url
    assert fresh["adapter"] == "openai-compatible"
    assert fresh["kind"] == "local"
    # What the model IS is resolved at read time too, so a fact the catalog
    # learns tomorrow appears on a deployment saved today with no migration.
    assert fresh["version"].model == "stub-model"
    assert fresh["version"].capabilities.get("vision") is Support.UNKNOWN


def test_a_model_cannot_be_repointed_at_another_connection_by_patching_it(stub):
    [model] = connect(stub)
    patched = deployments.update_deployment(model["id"], {"baseUrl": "http://evil", "label": "renamed"})
    assert patched["label"] == "renamed"
    assert patched["baseUrl"] == stub.base_url, "adapter/address must come from the connection"


def test_the_same_model_name_may_exist_under_two_connections(stub):
    a = connections.add_connection(adapter="openai-compatible", base_url=stub.base_url, label="one")
    b = connections.add_connection(adapter="openai-compatible", base_url=stub.base_url, label="two")
    deployments.add_deployment(connection_id=a["id"], model="stub-model")
    deployments.add_deployment(connection_id=b["id"], model="stub-model")
    assert len(deployments.list_deployments()) == 2
    with pytest.raises(ValueError):
        deployments.add_deployment(connection_id=a["id"], model="stub-model")


def test_deleting_a_connection_takes_its_models_with_it(stub):
    [model] = connect(stub)
    removed = deployments.delete_connection(deployments.get_deployment(model["id"])["connectionId"])
    assert removed == 1 and deployments.list_deployments() == []


# --- routing -----------------------------------------------------------------

def entry(model_id, **kw):
    """A candidate. `caps` and `tier` are gone — see `conftest.candidate`."""
    base = candidate(model_id, **{k: v for k, v in kw.items()
                                  if k in ("capabilities", "quality", "lifecycle",
                                           "context_tokens", "effort", "provider")})
    base.update({k: v for k, v in kw.items() if k not in (
        "capabilities", "quality", "lifecycle", "context_tokens", "effort", "provider")})
    return base


def sighted(**extra):
    return Capabilities(tools=Support.YES, vision=Support.YES, **extra)


def test_a_known_bad_model_never_outranks_one_that_works():
    good = entry("good", quality=1)
    bad = entry("bad", quality=5)
    availability.record("bad", "quota", detail="out of quota")
    ranked = build_candidates(Task(text="hello"), entries=[bad, good])
    assert [e["id"] for e in ranked] == ["good"], "a benched model must not be offered"


@pytest.mark.parametrize("role", list(Role))
@pytest.mark.parametrize("balance", ["fast", "balanced", "quality"])
def test_the_availability_bonus_still_outweighs_every_scoring_branch(role, balance):
    """`AVAILABILITY_BONUS` claims to exceed the widest spread `_score` can
    produce. Every input the formula reads changed at the switchover — quality
    now comes from the catalog, price from recorded spend, speed from recorded
    latency — so the claim has to be re-established rather than inherited.

    The worst case is the best conceivable dead model against the worst
    conceivable working one, in each branch.
    """
    best = entry("dead-but-perfect", quality=5,
                 capabilities=Capabilities(tools=Support.YES, vision=Support.YES))
    worst = entry("alive-but-poor", quality=0,
                  capabilities=Capabilities(tools=Support.YES, vision=Support.NO))
    for _ in range(latency.MIN_SAMPLES):
        latency.record("dead-but-perfect", 10)      # fastest bucket
        latency.record("alive-but-poor", 60_000)    # slowest bucket
    availability.record("dead-but-perfect", "quota", detail="out of quota")

    ranked = build_candidates(Task(text="write an essay", role=role),
                              balance=balance, entries=[best, worst])

    assert [e["id"] for e in ranked] == ["alive-but-poor"]


def test_ties_break_deterministically_not_on_file_order():
    a, b = entry("zeta", quality=3), entry("alpha", quality=3)
    assert [e["id"] for e in build_candidates(Task(), entries=[a, b])] == ["alpha", "zeta"]


def test_a_need_is_enforced_by_the_router_itself():
    """In the Node version the router had no concept of this, so the check lived
    in one caller and was missing from another entirely."""
    blind = entry("blind", capabilities=Capabilities(tools=Support.YES, vision=Support.NO))
    seeing = entry("seeing", capabilities=sighted())
    task = Task(text="what's in this picture", need={"vision": True})
    assert [e["id"] for e in build_candidates(task, entries=[blind, seeing])] == ["seeing"]
    assert explain_exclusions(task, entries=[blind, seeing])["counts"] == {"no_vision": 1}


def test_a_model_nobody_has_asked_about_is_still_offered_for_an_image():
    """The switchover's own change, and the one worth stating out loud.

    The old `caps` dict had two states, so "we have never established whether
    this model can see" arrived as `False` and the model was hidden with no
    visible reason — which is what made video and web search effectively
    Gemini-only whatever the user knew about their own roster. `UNKNOWN` is now
    offered and allowed to fail honestly, which is the only way anybody finds
    out. Only a definite NO excludes.
    """
    unasked = entry("unasked")                       # everything UNKNOWN
    refused = entry("refused", capabilities=Capabilities(vision=Support.NO))
    task = Task(text="what's in this picture", needs_tools=False, need={"vision": True})

    ranked = build_candidates(task, entries=[unasked, refused])

    assert [e["id"] for e in ranked] == ["unasked"]
    assert explain_exclusions(task, entries=[unasked, refused])["counts"] == {"no_vision": 1}


def test_a_model_the_provider_has_retired_is_not_offered_at_all():
    """A dropped model used to be invisible: it failed, cooled down, and was
    rediscovered as a dead end every few hours, forever."""
    gone = entry("gone", lifecycle=Lifecycle.RETIRED)
    here = entry("here")
    assert [e["id"] for e in build_candidates(Task(), entries=[gone, here])] == ["here"]
    assert explain_exclusions(Task(), entries=[gone, here])["counts"] == {"retired": 1}


def test_a_pin_leads_but_does_not_become_a_single_point_of_failure():
    a, b = entry("a"), entry("b")
    ranked = build_candidates(Task(), model_id="b", entries=[a, b])
    assert [e["id"] for e in ranked] == ["b", "a"]


def test_exclusions_are_explained_with_the_same_predicate_that_excluded():
    off = entry("off", enabled=False)
    toolless = entry("toolless", capabilities=Capabilities(tools=Support.NO))
    counts = explain_exclusions(Task(), entries=[off, toolless])["counts"]
    assert counts == {"disabled": 1, "no_tools": 1}


def test_a_measured_latency_is_what_makes_the_fast_dial_mean_anything():
    """`tier.speed` was a name regex and was deleted with the rest of them.

    Nothing replaced it until this: without a measured reading, "fast" and
    "balanced" would rank identically and the preference would silently do
    nothing. Both models here are equal on every other term, so the ordering is
    the measurement and nothing else.
    """
    quick, slow = entry("quick"), entry("slow")
    for _ in range(latency.MIN_SAMPLES):
        latency.record("quick", 150)
        latency.record("slow", 8000)

    ranked = build_candidates(Task(text="hi"), balance="fast", entries=[quick, slow])
    assert [e["id"] for e in ranked] == ["quick", "slow"]


def test_an_unmeasured_model_is_neither_rewarded_nor_punished_for_it():
    """A model nobody has timed sits at the neutral reading, so it competes on
    the terms something IS known about rather than being ranked last for having
    no history — which would mean a newly added model never got a first turn."""
    fresh, measured = entry("fresh"), entry("measured")
    for _ in range(latency.MIN_SAMPLES):
        latency.record("measured", 3000)            # slower than neutral

    ranked = build_candidates(Task(text="hi"), balance="fast", entries=[fresh, measured])
    assert [e["id"] for e in ranked] == ["fresh", "measured"]


def test_one_cold_start_does_not_decide_how_a_model_is_ranked():
    """A first call to a cold endpoint is routinely several times slower than
    every call after it, and a tier taken from that one reading would bench a
    fast model on its own warm-up."""
    latency.record("cold", 9000)
    assert latency.speed_tier("cold") is None, "one sample is not an average"
    latency.record("cold", 200)
    assert latency.speed_tier("cold") is not None


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


# --- effort, end to end ------------------------------------------------------

def test_the_level_a_role_asks_for_reaches_the_wire(stub):
    """The whole point of the slot's second half.

    Nothing between the setting and the request re-decides it: the slot names a
    level, the version's scheme says what that means here, and the adapter
    spells it. A control that stops somewhere in the middle is worse than no
    control, because it looks like it worked.
    """
    stub.says("thought about it")
    connect(stub, models=("gpt-5-mini",))
    slots.assign(Role.CONVERSATION, effort=Effort.HIGH)

    collect(Gateway(event_bus=EventBus()))

    sent = [r["body"] for r in stub.requests if r["path"].endswith("/chat/completions")]
    assert sent and sent[-1]["reasoning_effort"] == "high"


def test_nothing_is_sent_for_a_version_that_has_no_reasoning_control(stub):
    """An UNKNOWN scheme is not a scheme with a default. Sending a parameter on
    the chance it works would spend a failed round trip per turn on exactly the
    rosters — local, small, unlisted — least able to afford one."""
    stub.says("fine")
    connect(stub, models=("stub-model",))          # matches no catalog rule
    slots.assign(Role.CONVERSATION, effort=Effort.HIGH)

    collect(Gateway(event_bus=EventBus()))

    sent = [r["body"] for r in stub.requests if r["path"].endswith("/chat/completions")]
    assert sent and "reasoning_effort" not in sent[-1]


def test_a_clamp_is_announced_rather_than_applied_quietly(stub):
    """Asking for more than a version can take is the NORMAL case — the ladders
    genuinely differ between providers — so it lowers the request rather than
    failing the turn. But a clamp nobody can see is indistinguishable from the
    setting being ignored, which is how a control teaches people it does not
    work. Gemini's level enum stops at HIGH; the ladder does not.
    """
    stub.says("ok")
    connect(stub, models=("gemini-3-pro",))
    slots.assign(Role.CONVERSATION, effort=Effort.MAX)

    started = []
    ebus = EventBus()
    ebus.subscribe(EventType.MODEL_CALL_STARTED, lambda e: started.append(e.payload))
    collect(Gateway(event_bus=ebus))

    assert started[-1]["effort"] == "HIGH"
    assert started[-1]["effortRequested"] == "MAX"
    assert started[-1]["effortClamped"] is True


def test_an_honoured_level_is_reported_without_claiming_a_clamp(stub):
    """Guards the test above: a payload that always said `clamped` would make it
    pass while proving nothing."""
    stub.says("ok")
    connect(stub, models=("gemini-3-pro",))
    slots.assign(Role.CONVERSATION, effort=Effort.LOW)

    started = []
    ebus = EventBus()
    ebus.subscribe(EventType.MODEL_CALL_STARTED, lambda e: started.append(e.payload))
    collect(Gateway(event_bus=ebus))

    assert started[-1]["effort"] == "LOW"
    assert "effortClamped" not in started[-1]


def test_a_refused_parameter_is_paid_for_once_rather_than_every_turn(stub):
    """The learned refusal, through the real gateway rather than in isolation.

    A version whose scheme says TIERS can still be served by an endpoint that
    rejects the parameter. The retry costs one round trip; not remembering it
    would cost one on every turn for the life of the install.
    """
    stub.fails(400, "Unrecognized request argument supplied: reasoning_effort").says("second try")
    connect(stub, models=("gpt-5-mini",))
    slots.assign(Role.CONVERSATION, effort=Effort.HIGH)

    events = collect(Gateway(event_bus=EventBus()))
    assert "".join(e.text for e in events if isinstance(e, TextChunk)) == "second try"

    sent = [r["body"] for r in stub.requests if r["path"].endswith("/chat/completions")]
    assert "reasoning_effort" in sent[0] and "reasoning_effort" not in sent[1]

    # And the next turn does not re-learn it.
    stub.says("third")
    collect(Gateway(event_bus=EventBus()))
    sent = [r["body"] for r in stub.requests if r["path"].endswith("/chat/completions")]
    assert "reasoning_effort" not in sent[-1]


# --- the role reaches the routing -------------------------------------------

def test_a_spoken_turn_is_ranked_for_latency_and_a_typed_one_is_not(stub):
    """Before the switchover every turn arrived as a default text conversation
    however it had started, so the voice scoring branch was unreachable."""
    quick, careful = entry("quick", quality=1), entry("careful", quality=5)
    for _ in range(latency.MIN_SAMPLES):
        latency.record("quick", 100)
        latency.record("careful", 7000)

    # One question, asked two ways. `compare` is a reasoning hint, so the typed
    # branch weighs quality; the spoken branch never reaches that test at all.
    asked = "compare the two settlements and why the difference mattered"
    spoken = build_candidates(Task(text=asked, role=Role.VOICE), entries=[careful, quick])
    typed = build_candidates(Task(text=asked, role=Role.CONVERSATION), entries=[careful, quick])

    assert spoken[0]["id"] == "quick", "latency is most of a spoken reply's experience"
    assert typed[0]["id"] == "careful", "a typed question this long wants the better answer"


def test_the_balance_dial_is_read_per_turn_rather_than_at_startup(stub):
    """It used to be baked into the gateway when the orchestrator singleton was
    built, so changing it did nothing until the process restarted."""
    from jarvis import prefs

    quick, careful = entry("quick", quality=1), entry("careful", quality=5)
    for _ in range(latency.MIN_SAMPLES):
        latency.record("quick", 100)
        latency.record("careful", 7000)

    prefs.set_prefs({"balance": "quality"})
    assert build_candidates(Task(text="hi"), entries=[quick, careful])[0]["id"] == "careful"

    prefs.set_prefs({"balance": "fast"})
    assert build_candidates(Task(text="hi"), entries=[quick, careful])[0]["id"] == "quick"


def test_a_record_too_damaged_to_resolve_is_not_waved_through(stub):
    """A deployment with no resolvable version reads as "everything unknown",
    which is the same state as a model nobody has asked about — so it is still
    offered for an ordinary turn, and still refused for the one capability
    where unknown is not good enough."""
    broken = {"id": "broken", "model": "x", "enabled": True, "keyRequired": False}

    assert [e["id"] for e in build_candidates(Task(), entries=[broken])] == ["broken"]
    assert build_candidates(Task(need={"webSearch": True}), entries=[broken]) == []
