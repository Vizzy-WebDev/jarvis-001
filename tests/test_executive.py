"""The executive: what runs next, what a stuck job is told, and when the person is needed.

Real turn loop, executor, durable runner, job worker and supervisor; the model is
`durable_support.Brain`. Everything is read back from the job rows, the trace and the outbox.
"""

from __future__ import annotations

import threading

import pytest

from durable_support import Brain, effect_count, install, register_effects, restart, run_and_wait
from jarvis import assembly, background, conversation, durable
from jarvis.db import reset_for_tests as reset_db
from jarvis.events.bus import EventBus
from jarvis.jobs import job_store, objective, orchestrator, worker
from jarvis.jobs.policy import (
    RECOVERY_LIMITS, holds_a_slot, made_progress, may_extend, may_recover, next_in_line,
    recovery_kind,
)
from jarvis.orchestrator.pipeline import MAX_STEPS
from jarvis.prefs import set_prefs


@pytest.fixture(autouse=True)
def _isolate(scratch, monkeypatch):
    reset_db()
    conversation.reset_for_tests()
    assembly.reset_for_tests()
    durable.reset_for_tests()
    monkeypatch.setattr("jarvis.jobs.worker._verify_result", lambda job, answer: None)
    register_effects()
    yield
    assert worker.join_all(timeout=30), "a job worker was still running at the end"
    assert background.join_all(timeout=20)
    durable.reset_for_tests()
    assembly.reset_for_tests()
    conversation.reset_for_tests()
    reset_db()


def lookups(n: int, start: int = 0) -> list[tuple]:
    return [("call", "look_up_fact", {"q": f"fact {i}"}) for i in range(start, start + n)]


def outbox_for(job_id: str) -> list[dict]:
    return [o for o in job_store.list_pending_outbox(max_tier=3) if o["jobId"] == job_id]


# --- the rules, as truth tables -------------------------------------------------------------------

@pytest.mark.parametrize("job,live,waiting,expected", [
    ({"status": "running"}, False, False, True),
    ({"status": "queued"}, True, False, True),            # a worker has it
    ({"status": "queued"}, False, True, True),            # only paused until a model answers
    ({"status": "queued"}, False, False, False),          # in line
    ({"status": "awaiting_decision"}, False, False, False),   # an unanswered question holds no place
    ({"status": "stalled"}, False, False, False),
    ({"status": "done"}, False, False, False),
])
def test_what_counts_against_how_many_may_work_at_once(job, live, waiting, expected):
    assert holds_a_slot(job, live=live, waiting_for_model=waiting) is expected


def test_the_line_is_most_important_first_then_first_come():
    line = next_in_line([{"id": "c", "priority": 2, "createdAt": "2"}, {"id": "a", "priority": 1, "createdAt": "3"},
                         {"id": "b", "priority": 2, "createdAt": "1"}])
    assert [j["id"] for j in line] == ["a", "b", "c"]


@pytest.mark.parametrize("cause,kind", [
    ("did not match the request", "check"), ("hung", "crash"), ("crashed", "crash"),
    ("failed", "approach"), ("exact_repeat", "approach"), ("oscillation", "approach"),
    ("repeated_failure", "approach"), ("stalled", "approach"), (None, "approach"),
])
def test_why_work_stopped_decides_how_it_is_set_going_again(cause, kind):
    assert recovery_kind(cause) == kind


def test_each_kind_of_trouble_has_its_own_allowance():
    job = {"attempts": {"approach": RECOVERY_LIMITS["approach"], "check": 1}}
    assert may_recover(job, "approach") is False
    assert may_recover(job, "check") is True and may_recover(job, "crash") is True
    assert may_recover({}, "approach") is True


@pytest.mark.parametrize("before,now,expected", [
    ({}, {"done": 1}, True), ({"done": 1}, {"done": 1}, False),
    ({"found": 2}, {"found": 3}, True),
    ({"calls": 4}, {"calls": 6}, False), ({"calls": 4}, {"calls": 7}, True),
    (None, {}, False),
])
def test_progress_is_a_step_done_something_found_or_several_things_that_worked(before, now, expected):
    assert made_progress(before, now) is expected


def test_extensions_are_given_for_progress_and_never_without_limit():
    assert may_extend({"attempts": {}}, {"calls": 5}) is True
    assert may_extend({"attempts": {"extensions": 3, "mark": {}}}, {"calls": 50}) is False
    assert may_extend({"attempts": {"extensions": 1, "mark": {"calls": 9}}}, {"calls": 10}) is False


# --- what runs next -------------------------------------------------------------------------------

def test_work_beyond_what_may_run_at_once_waits_in_line_and_starts_in_order_of_importance():
    set_prefs({"maxBackgroundJobs": 1})
    brain = install(Brain())
    gate = threading.Event()
    order: list[str] = []
    real = Brain.stream

    def stream(self, **kw):
        if kw["session_id"].startswith("job:"):
            job = job_store.get_job(kw["session_id"][4:])
            if job["title"] not in order:
                order.append(job["title"])
            if job["title"] == "first":
                assert gate.wait(20)
        return real(self, **kw)

    brain.stream = stream.__get__(brain)  # type: ignore[method-assign]
    brain.plan("job:", [[("say", "done")]])

    first = orchestrator.admit(title="first", goal="a", event_bus=EventBus())
    routine = orchestrator.admit(title="routine", goal="b", priority=3, event_bus=EventBus())
    urgent = orchestrator.admit(title="urgent", goal="c", priority=1, event_bus=EventBus())

    assert "waitingBehind" not in first
    assert routine["waitingBehind"] == ["first"] and urgent["waitingBehind"] == ["first"]
    assert [j["title"] for j in orchestrator.in_line()] == ["urgent", "routine"]
    assert [j["title"] for j in orchestrator.working()] == ["first"]

    gate.set()
    assert worker.join_all(timeout=30)
    # Each finishing made room for the next, with no clock involved: all three are done, in order.
    for _ in range(3):
        worker.join_all(timeout=30)
    assert [j["status"] for j in job_store.list_jobs()] == ["done", "done", "done"]
    assert order == ["first", "urgent", "routine"]


def test_a_job_waiting_on_the_person_does_not_hold_up_other_work():
    set_prefs({"maxBackgroundJobs": 1})
    brain = install(Brain())
    job_store.create_job(title="needs you", goal="x", status="awaiting_decision")
    brain.plan("job:", [[("say", "done")]])
    started = orchestrator.admit(title="can go", goal="y", event_bus=EventBus())
    assert "waitingBehind" not in started
    assert worker.join_all(timeout=20)
    assert job_store.get_job(started["id"])["status"] == "done"


def test_a_restart_starts_no_more_at_once_than_may_work_at_once():
    set_prefs({"maxBackgroundJobs": 1})
    brain = install(Brain())
    gate = threading.Event()
    seen: list[str] = []
    real = Brain.stream

    def stream(self, **kw):
        seen.append(kw["session_id"])
        assert gate.wait(20)
        return real(self, **kw)

    brain.stream = stream.__get__(brain)  # type: ignore[method-assign]
    brain.plan("job:", [[("say", "done")]])
    for n in range(3):
        job_store.create_job(title=f"queued {n}", goal="g")       # what a dead process left

    orchestrator.recover_orphans(event_bus=EventBus())
    assert len(orchestrator.working()) == 1 and len(orchestrator.in_line()) == 2
    gate.set()
    for _ in range(4):
        worker.join_all(timeout=30)
    assert {j["status"] for j in job_store.list_jobs()} == {"done"}


# --- set going again by why it stopped --------------------------------------------------------------

def test_going_round_in_circles_is_told_to_take_a_different_way_twice_then_goes_to_the_person():
    brain = install(Brain())
    job = job_store.create_job(title="Trail", goal="follow it")
    objective.update_ledger(job["id"], steps=[{"text": "read the first entry", "status": "done"},
                                              {"text": "read the rest", "status": "doing"}])
    job_store.update_job(job["id"], {"status": "stalled", "error": "The last three attempts all failed."})
    job_store.append_trace(job["id"], phase="outcome", effect="read", kind="note",
                           summary="stalled: failed", detail="The last three attempts all failed.")
    brain.plan("job:", [[("say", "x")]])
    told: list[str] = []

    def fail_again(job_id, **kw):
        told.append(str(kw.get("answer")))
        job_store.update_job(job_id, {"status": "stalled", "error": "The last three attempts all failed."})

    import jarvis.jobs.worker as worker_module
    real = worker_module.run_in_background
    worker_module.run_in_background = lambda job_id, **kw: fail_again(job_id, **kw)  # type: ignore[assignment]
    try:
        actions = [orchestrator.supervise(event_bus=EventBus())[0] for _ in range(3)]
    finally:
        worker_module.run_in_background = real  # type: ignore[assignment]

    assert [a["action"] for a in actions] == ["retried", "retried", "escalated"]
    assert all("do NOT repeat it" in t and "different way" in t for t in told) and len(told) == 2
    stored = job_store.get_job(job["id"])
    assert stored["attempts"] == {"approach": 2} and stored["retries"] == 2
    assert stored["status"] == "awaiting_decision"
    [ask] = [o for o in outbox_for(job["id"]) if o["reason"] == "stuck"]
    # A specific account: what stopped it, what was tried, where it stands.
    assert "The last three attempts all failed." in ask["summary"]
    assert "I tried 2 different ways round it" in ask["summary"]
    assert "Done so far: read the first entry" in ask["summary"]
    assert ask["summary"].endswith("Tell me how to go on, or to stop.")


def test_a_result_sent_back_by_the_check_is_told_exactly_what_to_put_right(monkeypatch):
    """And it does not use up the allowance for getting nowhere: they are different troubles."""
    brain = install(Brain())
    job = job_store.create_job(title="Total", goal="give the total")
    verdicts = iter([type("V", (), {"failed": True, "reason": "it does not state the total"})(), None])
    monkeypatch.setattr("jarvis.jobs.worker._verify_result", lambda job, answer: next(verdicts))
    brain.plan("job:", [[("say", "Filed it.")], [("say", "Filed it: the total is 5.")]])

    assert run_and_wait(lambda: worker.run_job(job["id"], event_bus=EventBus()))["status"] == "stalled"
    [action] = orchestrator.supervise(event_bus=EventBus())
    assert (action["action"], action["kind"]) == ("retried", "check")
    assert worker.join_all(timeout=20)

    stored = job_store.get_job(job["id"])
    assert stored["status"] == "done" and stored["result"] == "Filed it: the total is 5."
    assert stored["attempts"] == {"check": 1}
    second = brain.calls_for(f"job:{job['id']}", 1)[0]["messages"][-1]["text"]
    assert "checked against what was asked and sent back" in second
    assert "it does not state the total" in second and "did not get anywhere" not in second


def test_a_hung_job_is_picked_up_from_where_its_plan_stands():
    brain = install(Brain())
    job = job_store.create_job(title="Hung", goal="g", status="running")
    job_store.update_job(job["id"], {"heartbeatAt": "2000-01-01T00:00:00.000Z"})
    brain.plan("job:", [[("say", "Picked up and finished.")]])
    [action] = orchestrator.supervise(event_bus=EventBus())
    assert (action["action"], action["kind"]) == ("retried", "crash")
    assert worker.join_all(timeout=20)
    stored = job_store.get_job(job["id"])
    assert stored["attempts"] == {"crash": 1} and stored["status"] == "done"
    assert stored["result"] == "Picked up and finished."


def test_a_stopped_round_never_overwrites_what_the_job_has_become(monkeypatch):
    """Found by a live run: a job stopped for repeating itself and set going again was marked
    "waiting on you" — with no question anywhere — by the stopped round's own thread."""
    install(Brain())
    job = job_store.create_job(title="Raced", goal="g")

    def stopped_while_another_run_took_over(job_id, kind, text, **kw):
        # What the executive does between stopping the round and this thread returning:
        job_store.update_job(job_id, {"status": "running", "currentStep": "working"})
        return {"state": "waiting", "why": {"reason": "stopped", "steps": 3}}

    monkeypatch.setattr(durable, "advance", stopped_while_another_run_took_over)
    worker.run_job(job["id"], event_bus=EventBus())
    stored = job_store.get_job(job["id"])
    assert stored["status"] == "running" and stored["currentStep"] == "working"


def test_a_job_repeating_itself_while_running_is_stopped_and_set_going_again_cleanly():
    """The whole path, with a real held round: the clock sees the repetition, stops the round
    and sets the job going again; the job ends done, and is never shown as waiting on anyone."""
    brain = install(Brain())
    job = job_store.create_job(title="Repeats", goal="read it")
    session = f"job:{job['id']}"
    same = ("call", "look_up_fact", {"q": "the same thing"})
    # Round 0 repeats itself and is then held mid-round; the round after the nudge finishes.
    brain.plan("job:", [[same, same, same, same, same], [("say", "Took another way: done.")]])
    brain.hold[(session, 0, 4)] = threading.Event()
    brain.reached[(session, 0, 4)] = threading.Event()

    worker.run_in_background(job["id"], event_bus=EventBus())
    assert brain.reached[(session, 0, 4)].wait(20)
    seen: list[str] = []
    real = job_store.update_job

    def watch(job_id, patch):
        if "status" in patch:
            seen.append(patch["status"])
        return real(job_id, patch)

    job_store.update_job = watch  # type: ignore[assignment]
    try:
        [action] = [a for a in orchestrator.supervise(event_bus=EventBus()) if a["action"] == "retried"]
        assert action["cause"] == "exact_repeat" and action["kind"] == "approach"
        brain.hold[(session, 0, 4)].set()
        for _ in range(3):
            assert worker.join_all(timeout=30)
    finally:
        job_store.update_job = real  # type: ignore[assignment]

    stored = job_store.get_job(job["id"])
    assert stored["status"] == "done" and stored["result"] == "Took another way: done."
    assert "awaiting_decision" not in seen
    assert outbox_for(job["id"]) and all(o["reason"] == "finished" for o in outbox_for(job["id"]))


# --- saying it cannot be done -----------------------------------------------------------------------

def test_work_that_says_it_cannot_be_finished_goes_to_the_person_and_not_back_through_the_check(monkeypatch):
    """Found by a live run: a real model correctly concluded a record could not be read, and
    the completion check rejected that honest answer as "incomplete" and sent it back."""
    brain = install(Brain())
    checked: list[str] = []
    monkeypatch.setattr("jarvis.jobs.worker._verify_result",
                        lambda job, answer: checked.append(answer) or None)
    job = job_store.create_job(title="Trail", goal="follow the trail and file the total")
    brain.plan("job:", [
        [("call", "update_plan", {"steps": [{"text": "read entries 1 to 4", "status": "done"},
                                            {"text": "read entry 5", "status": "blocked"}]}),
         ("call", "cannot_finish", {"reason": "entry 5 will not open", "tried": ["reading it again", "searching for a copy"]}),
         ("say", "I could not finish: entry 5 will not open, so no total was filed.")],
        [("call", "effect_low", {"tag": "filed-after-guidance"}), ("say", "Filed with the first four entries, as you said.")]])

    result = run_and_wait(lambda: worker.run_job(job["id"], event_bus=EventBus()))

    assert result == {"status": "awaiting_decision", "reason": "blocked", "error": "entry 5 will not open"}
    stored = job_store.get_job(job["id"])
    assert stored["status"] == "awaiting_decision" and stored["retries"] == 0 and not stored["attempts"]
    assert checked == []                                         # never put through the check
    [ask] = outbox_for(job["id"])
    assert ask["tier"] == 1 and ask["reason"] == "blocked"
    assert 'could not be finished: entry 5 will not open' in ask["summary"]
    assert "I tried: reading it again; searching for a copy." in ask["summary"]
    assert "Done so far: read entries 1 to 4." in ask["summary"]
    assert orchestrator.supervise(event_bus=EventBus()) == []    # and it is not "recovered" either
    assert durable.get(job["id"])["status"] == "waiting"         # its place is kept

    # The person says how to go on: it carries on from there, and finishes.
    orchestrator.resume(job["id"], guidance="File it with the first four entries.", event_bus=EventBus())
    assert worker.join_all(timeout=20)
    stored = job_store.get_job(job["id"])
    assert stored["status"] == "done" and effect_count("filed-after-guidance") == 1
    assert objective.blocked_of(stored) is None
    assert "File it with the first four entries." in brain.calls_for(f"job:{job['id']}", 1)[0]["messages"][-1]["text"]


def test_background_work_cannot_start_more_background_work_through_a_specialist():
    """Found by a live run: a job handed its three parts to specialists "in the background" —
    three more jobs nothing waited for, each stopping to ask the person something."""
    from jarvis.agents import capabilities as agent_capabilities, ensure_builtins
    from jarvis.policy import Autonomy, CallContext, Surface

    install(Brain())
    ensure_builtins()
    job = job_store.create_job(title="T", goal="g")
    inside = CallContext(session_id=f"job:{job['id']}", turn_id="t", surface=Surface.JOB,
                         autonomy=Autonomy.PRE_CONSENTED)
    refused = agent_capabilities._ask(ctx=inside, agent="operations", task="do a part", background=True)
    assert refused["ok"] is False and "already working in the background" in refused["error"]
    assert len(job_store.list_jobs()) == 1


def test_a_model_being_asked_or_answering_is_a_sign_of_life_not_a_hung_job(monkeypatch):
    """Found live: only a finished tool call counted, so a job whose model took 100 seconds a
    reply was declared hung, counted as a crash and restarted while still working."""
    from jarvis.events.bus import EventType
    from jarvis.jobs.policy import is_hung

    job = job_store.create_job(title="T", goal="g")
    job_store.update_job(job["id"], {"status": "running", "heartbeatAt": "2020-01-01T00:00:00.000Z"})
    now_ms = __import__("time").time() * 1000
    assert is_hung(job_store.get_job(job["id"]), now_ms=now_ms, timeout_ms=orchestrator.HANG_TIMEOUT_MS)

    worker._last_alive.clear()
    worker._observe(job["id"], EventType.MODEL_CALL_STARTED.value, {"sessionId": f"job:{job['id']}"})
    assert not is_hung(job_store.get_job(job["id"]), now_ms=now_ms, timeout_ms=orchestrator.HANG_TIMEOUT_MS)

    # ...written sparingly: a streamed reply is hundreds of events.
    beats = []
    monkeypatch.setattr(job_store, "heartbeat", lambda job_id, **kw: beats.append(job_id))
    for _ in range(50):
        worker._on_event(job["id"], object())
    assert beats == []
    worker._last_alive.clear()
    worker._on_event(job["id"], object())
    assert beats == [job["id"]]


def test_cannot_finish_is_a_workers_tool_and_needs_a_reason():
    from jarvis.policy import Autonomy, CallContext, Surface

    install(Brain())
    spec = assembly.get_registry().get("cannot_finish")
    assert spec.has_tag("job")
    job = job_store.create_job(title="T", goal="g")
    inside = CallContext(session_id=f"job:{job['id']}", turn_id="t", surface=Surface.JOB, autonomy=Autonomy.PRE_CONSENTED)
    outside = CallContext(session_id="chat-1", turn_id="t", surface=Surface.TEXT, autonomy=Autonomy.INTERACTIVE)
    assert spec.handler(reason="x", ctx=outside)["ok"] is False
    assert spec.handler(reason=" ", ctx=inside)["ok"] is False
    assert spec.handler(reason="it is locked", ctx=inside)["ok"] is True
    assert objective.blocked_of(job_store.get_job(job["id"]))["reason"] == "it is locked"


# --- a step allowance that follows the work --------------------------------------------------------

def test_a_job_out_of_steps_but_getting_somewhere_carries_on_without_asking(monkeypatch):
    monkeypatch.setitem(worker.STEP_BUDGET_BY_KIND, "generic", MAX_STEPS)
    brain = install(Brain())
    job = job_store.create_job(title="Long", goal="read them all")
    # Three allowances' worth of distinct, successful work, then the finish.
    brain.plan("job:", [lookups(MAX_STEPS, 0), lookups(MAX_STEPS, 20),
                        lookups(3, 40) + [("call", "effect_low", {"tag": "filed"}), ("say", "All read and filed.")]])

    result = run_and_wait(lambda: worker.run_job(job["id"], event_bus=EventBus()))

    assert result["status"] == "done"
    stored = job_store.get_job(job["id"])
    assert stored["status"] == "done" and stored["attempts"]["extensions"] == 2
    assert effect_count("filed") == 1
    assert [o["reason"] for o in outbox_for(job["id"])] == ["finished"]      # nobody was asked anything
    assert [r["kind"] for r in job_store.get_trace(job["id"])].count("extend") == 2


def test_a_job_out_of_steps_and_going_nowhere_asks(monkeypatch):
    monkeypatch.setitem(worker.STEP_BUDGET_BY_KIND, "generic", MAX_STEPS)
    brain = install(Brain())
    job = job_store.create_job(title="Loop", goal="g")
    same = [("call", "look_up_fact", {"q": "the same thing"})] * (MAX_STEPS - 1)
    brain.plan("job:", [same, same])

    result = run_and_wait(lambda: worker.run_job(job["id"], event_bus=EventBus()))

    assert result == {"status": "awaiting_decision", "reason": "budget", "steps": MAX_STEPS}
    assert not job_store.get_job(job["id"])["attempts"].get("extensions")
    [ask] = [o for o in outbox_for(job["id"])]
    assert ask["reason"] == "budget" and "Keep going?" in ask["summary"]


def test_extensions_run_out_and_then_it_asks(monkeypatch):
    monkeypatch.setattr("jarvis.jobs.policy.MAX_AUTO_EXTENSIONS", 1)
    monkeypatch.setitem(worker.STEP_BUDGET_BY_KIND, "generic", MAX_STEPS)
    brain = install(Brain())
    job = job_store.create_job(title="Endless", goal="g")
    brain.plan("job:", [lookups(MAX_STEPS, 0), lookups(MAX_STEPS, 20), lookups(MAX_STEPS, 40)])

    result = run_and_wait(lambda: worker.run_job(job["id"], event_bus=EventBus()))

    assert result["status"] == "awaiting_decision" and result["reason"] == "budget"
    assert job_store.get_job(job["id"])["attempts"]["extensions"] == 1
    assert [o["reason"] for o in outbox_for(job["id"])] == ["budget"]
