"""Background jobs (§13, §32, §43).

The policy half is a truth table — it runs on every supervisor tick for every
job, so it has to be free, and being pure is what makes it checkable.

The rest is driven through the real worker and orchestrator against a stub
model, because the claims that matter are about what actually happens to a job:
that a decision parks instead of asking, that one retry means one, that a crash
mid-external-action is never silently repeated, and that the desktop never
starts on its own.
"""

from __future__ import annotations

import json

import pytest

from jarvis import assembly, conversation
from jarvis.capabilities import CapabilityRegistry, CapabilitySpec, Risk
from jarvis.capabilities.execute import ExecOutcome, execute
from jarvis.db import reset_for_tests as reset_db
from jarvis.events import EventType
from jarvis.events.bus import EventBus
from jarvis.gateway import availability, connections, registry as model_registry
from jarvis.jobs import job_store, orchestrator, worker
from jarvis.jobs.policy import (
    can_auto_retry, classify_recovery, diagnose_stall, has_capacity, is_hung,
    resource_available, step_budget_exceeded, text_similarity,
)
from jarvis.policy import Autonomy, CallContext, Surface
from jarvis.policy import approvals as approval_store
from jarvis.tools import load_tools

from stub_openai_server import StubModelServer


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    conversation.reset_for_tests()
    availability.reset_for_tests()
    assembly.reset_for_tests()
    yield
    # Before the database is torn down: a worker still writing to it while the
    # connection closes is a segfault, not an exception.
    worker.join_all(timeout=10)
    assembly.reset_for_tests()
    availability.reset_for_tests()
    conversation.reset_for_tests()
    reset_db()


@pytest.fixture
def stub():
    server = StubModelServer()
    server.base_url = server.start()
    conn = connections.add_connection(adapter="openai-compatible", base_url=server.base_url,
                                      label="stub", provider="custom", kind="local",
                                      key_required=False)
    model_registry.add_model(connection_id=conn["id"], model="stub-model")
    yield server
    server.stop()


def tool_trace(name: str, args: dict) -> dict:
    return {"kind": "tool", "phase": "intent", "detail": json.dumps({"name": name, "args": args})}


# --- the policy, as a truth table --------------------------------------------

def test_a_crash_part_way_through_something_external_is_never_repeated():
    """The write-ahead intent alone condemns it: a crash before the action and a
    crash after it look identical from here, and retrying risks doing it twice."""
    for phase in ("intent", "outcome"):
        trace = [{"effect": "external", "phase": phase}]
        assert classify_recovery({"status": "running"}, trace) == "unrecoverable"


def test_a_job_that_only_read_things_can_simply_be_picked_back_up():
    assert classify_recovery({"status": "running"}, [{"effect": "read"}]) == "resumable"


def test_a_job_that_touched_its_own_workspace_starts_over():
    assert classify_recovery({"status": "running"}, [{"effect": "workspace"}]) == "restartable"


def test_a_job_parked_on_a_decision_still_needs_that_decision():
    assert classify_recovery({"status": "awaiting_decision"}, []) == "needs_input"


def test_the_same_call_three_times_is_a_stall():
    stall = diagnose_stall([tool_trace("search", {"q": "x"})] * 3)
    assert stall["cause"] == "exact_repeat"


def test_argument_order_does_not_hide_a_repeat():
    """A signature that changed only because a dict serialised differently is not
    progress."""
    rows = [tool_trace("search", {"a": 1, "b": 2}), tool_trace("search", {"b": 2, "a": 1}),
            tool_trace("search", {"a": 1, "b": 2})]
    assert diagnose_stall(rows)["cause"] == "exact_repeat"


def test_cycling_between_two_actions_is_a_stall():
    rows = [tool_trace("a", {}), tool_trace("b", {})] * 3
    assert diagnose_stall(rows)["cause"] == "oscillation"


def test_three_failures_in_a_row_is_a_stall():
    rows = [{"kind": "tool", "phase": "outcome",
             "detail": json.dumps({"name": "x", "ok": False})}] * 3
    assert diagnose_stall(rows)["cause"] == "repeated_failure"


def test_a_worker_restating_itself_with_no_tool_calls_is_caught():
    """The one failure mode with no tool calls at all, so nothing else sees it."""
    rows = [
        {"kind": "note", "detail": "I should check the flight prices and compare them "
                                   "across the three sites."},
        {"kind": "note", "detail": "I need to check the flight prices and compare them "
                                   "across the three sites."},
    ]
    assert diagnose_stall(rows)["cause"] == "near_duplicate_reasoning"


def test_a_genuinely_new_direction_is_not_a_stall():
    """The threshold has to leave room for a worker that changed its mind: a
    note that adds a real new step is progress, not repetition."""
    rows = [
        {"kind": "note", "detail": "I should check the flight prices on the three sites."},
        {"kind": "note", "detail": "The prices are all similar, so the deciding factor is "
                                   "baggage allowance. I will look at that next."},
    ]
    assert diagnose_stall(rows) is None


def test_real_progress_is_not_diagnosed_as_a_stall():
    rows = [tool_trace("a", {}), tool_trace("b", {}), tool_trace("c", {})]
    assert diagnose_stall(rows) is None
    assert diagnose_stall([]) is None


def test_similarity_is_not_fooled_by_length_alone():
    assert text_similarity("completely different words here", "nothing alike at all") < 0.5
    assert text_similarity("the same sentence", "the same sentence") == 1.0


def test_capacity_and_resources_are_hard_limits():
    assert has_capacity([{}, {}], 3) is True
    assert has_capacity([{}, {}, {}], 3) is False
    assert resource_available([{"resource": "computer"}], "computer") is False
    assert resource_available([{"resource": "computer"}], None) is True


def test_one_retry_means_one():
    """A crash retry and a stall retry share the counter, so a job cannot get two
    attempts by failing in two different ways."""
    assert can_auto_retry({"retries": 0}) is True
    assert can_auto_retry({"retries": 1}) is False


def test_a_step_budget_backstops_a_worker_that_never_converges():
    assert step_budget_exceeded(30, "research") is True
    assert step_budget_exceeded(19, "files") is False


def test_silence_is_only_a_hang_for_a_running_job():
    beat = "2020-01-01T00:00:00.000Z"
    running = {"status": "running", "heartbeatAt": beat}
    assert is_hung(running, now_ms=9e12, timeout_ms=1000) is True
    assert is_hung({"status": "queued", "heartbeatAt": beat}, now_ms=9e12, timeout_ms=1000) is False


# --- the store ---------------------------------------------------------------

def test_progress_is_absent_until_the_work_reports_one():
    """0% reads as stuck, which is a different thing from "hasn't said yet"."""
    job = job_store.create_job(title="t", goal="g")
    assert job["progress"] is None
    job_store.heartbeat(job["id"], step="looking things up", progress=40)
    assert job_store.get_job(job["id"])["progress"] == 40


def test_a_job_trace_has_its_own_sequence():
    a = job_store.create_job(title="a", goal="g")
    b = job_store.create_job(title="b", goal="g")
    for _ in range(3):
        job_store.append_trace(a["id"], phase="intent", effect="read", kind="note", summary="x")
    job_store.append_trace(b["id"], phase="intent", effect="read", kind="note", summary="y")
    assert [r["seq"] for r in job_store.get_trace(a["id"])] == [1, 2, 3]
    assert [r["seq"] for r in job_store.get_trace(b["id"])] == [1]


def test_tier_three_never_reaches_the_interruption_queue():
    job = job_store.create_job(title="t", goal="g")
    job_store.add_outbox(tier=3, summary="finished", job_id=job["id"])
    assert job_store.list_pending_outbox() == []
    job_store.add_outbox(tier=1, summary="needs you", job_id=job["id"])
    assert len(job_store.list_pending_outbox()) == 1


# --- the worker and the orchestrator -----------------------------------------

def test_a_job_runs_and_reports_what_it_produced(stub):
    stub.says("Found three flights under 400.")
    job = job_store.create_job(title="Find flights", goal="find flights to Lagos")
    result = worker.run_job(job["id"], event_bus=EventBus())

    assert result["status"] == "done"
    finished = job_store.get_job(job["id"])
    assert finished["result"] == "Found three flights under 400."
    assert finished["progress"] == 100


def test_a_worker_never_writes_into_the_conversation_the_user_is_looking_at(stub):
    stub.says("Done.")
    conversation.push_user_text("main", "what's the weather")
    job = job_store.create_job(title="t", goal="do the thing")
    worker.run_job(job["id"], event_bus=EventBus())

    assert [m["text"] for m in conversation.get_messages("main")] == ["what's the weather"]
    assert conversation.get_messages(worker.session_for(job["id"]))


def test_a_decision_parks_the_job_instead_of_asking_nobody(stub):
    """A job may wait hours. A confirmation token would be long expired, so the
    durable record is the job's status and an outbox row."""
    assembly.get_registry().register(CapabilitySpec(
        id="test.send", name="send_message", description="send",
        input_schema={"type": "object", "properties": {}},
        risk=Risk.HIGH, handler=lambda **_: "sent"))
    stub.calls_tool("send_message", {})

    job = job_store.create_job(title="Send the note", goal="send the note")
    result = worker.run_job(job["id"], event_bus=EventBus())

    assert result["status"] == "awaiting_decision"
    assert job_store.get_job(job["id"])["status"] == "awaiting_decision"
    parked = job_store.list_pending_outbox()
    assert len(parked) == 1 and parked[0]["tier"] == 1
    assert approval_store.pending()[0].surface == "job"


def test_a_stalled_job_gets_one_retry_and_then_the_user(stub):
    job = job_store.create_job(title="Stuck thing", goal="g")
    job_store.update_job(job["id"], {"status": "stalled", "error": "went nowhere"})

    stub.says("Fine now.")
    first = orchestrator.supervise(event_bus=EventBus())
    assert first[0]["action"] == "retried"
    assert job_store.get_job(job["id"])["retries"] == 1

    # The retry runs the job on its own thread. Waiting for it before forcing the
    # second stall is what makes this test about the retry policy rather than
    # about which of two threads happened to write the status last.
    worker.join_all()
    job_store.update_job(job["id"], {"status": "stalled", "error": "went nowhere again"})
    second = orchestrator.supervise(event_bus=EventBus())
    assert second[0]["action"] == "escalated"
    assert job_store.get_job(job["id"])["status"] == "awaiting_decision"


def test_the_desktop_never_starts_on_its_own(stub):
    """Autonomously operating the real machine is exactly the outward-facing,
    hard-to-undo work that has to come back to the owner first."""
    job = orchestrator.admit(title="Tidy the desktop", goal="tidy up", kind="computer",
                             event_bus=EventBus())
    assert job["status"] == "awaiting_decision"
    assert job["startedAt"] is None
    assert job_store.list_pending_outbox()[0]["reason"] == "permission"


def test_only_one_job_may_hold_the_desktop(stub):
    orchestrator.admit(title="First", goal="g", kind="computer", event_bus=EventBus())
    with pytest.raises(orchestrator.AtCapacity):
        orchestrator.admit(title="Second", goal="g", kind="computer", event_bus=EventBus())


def test_an_orphan_that_did_something_external_is_never_silently_restarted():
    job = job_store.create_job(title="Sent something", goal="g", status="running")
    job_store.append_trace(job["id"], phase="intent", effect="external", kind="tool",
                           summary="send email")
    [outcome] = orchestrator.recover_orphans(event_bus=EventBus())

    assert outcome["recovery"] == "unrecoverable"
    assert job_store.get_job(job["id"])["status"] == "awaiting_decision"
    assert "cannot safely be repeated" in job_store.list_pending_outbox()[0]["summary"]


# --- the tools ---------------------------------------------------------------

@pytest.fixture
def reg():
    registry = CapabilityRegistry()
    load_tools(registry)
    return registry


def ctx(turn="t1") -> CallContext:
    return CallContext(session_id="s1", turn_id=turn, surface=Surface.TEXT,
                       autonomy=Autonomy.INTERACTIVE)


def test_backgrounding_is_read_back_before_it_starts(reg):
    result = execute("work_in_background", {"goal": "find flights to Lagos"}, ctx(),
                     registry=reg)
    assert result.outcome is ExecOutcome.NEEDS_APPROVAL
    assert "find flights to Lagos" in approval_store.get(result.approval_id).reason
    assert job_store.list_active_jobs() == []


def test_being_at_capacity_names_what_is_already_running_rather_than_queueing(reg):
    for i in range(orchestrator.MAX_ACTIVE_JOBS):
        job_store.create_job(title=f"Job {i}", goal="g")
    from jarvis.tools.job_tools import _start

    result = _start(goal="one more thing")
    assert result["ok"] is False
    assert "Job 0" in result["error"] and "should I drop" in result["error"]


def test_checking_on_nothing_says_so_rather_than_inventing_a_status(reg):
    result = execute("check_on_work", {}, ctx(), registry=reg)
    assert result.ok and result.value["jobs"] == []


def test_stopping_names_the_job_and_how_far_it_got(reg):
    job = job_store.create_job(title="Find flights", goal="g")
    job_store.heartbeat(job["id"], progress=60)
    result = execute("stop_working_on", {"which": "flights"}, ctx(), registry=reg)
    reason = approval_store.get(result.approval_id).reason
    assert 'Stop working on "Find flights"?' in reason and "60%" in reason
    assert job_store.get_job(job["id"])["status"] != "cancelled"


def test_an_ambiguous_stop_refuses_rather_than_guessing(reg):
    job_store.create_job(title="First thing", goal="g")
    job_store.create_job(title="Second thing", goal="g")
    from jarvis.tools.job_tools import _stop

    assert _stop(which="")["ok"] is False, "guessing is how the wrong job gets cancelled"
