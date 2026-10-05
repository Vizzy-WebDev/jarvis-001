"""Durable background work (`jarvis/durable.py`) — what it does, asserted from the record.

Everything here is real except the model: the real turn loop, the real executor and its
`operations` table, real LangGraph checkpoints in a real file in the scratch dir, the real job
worker, supervisor, routes and trace. The model is `durable_support.Brain`, which answers from
the transcript it is sent (so a replayed round is asked the same and answers the same).

Every claim is read back from what was stored — the job row, its trace, the outbox, the
operations table, the rounds the runner recorded, the effect counter file, the model's own
call log — never from a return value alone. Every wait has a hard deadline.
"""

from __future__ import annotations

import json
import threading

import pytest

from durable_support import (
    Brain, effect_count, install, register_effects, restart, run_and_wait,
)
from jarvis import assembly, background, conversation, durable
from jarvis.db import get_db, reset_for_tests as reset_db
from jarvis.events.bus import EventBus
from jarvis.heartbeat import outbox
from jarvis.jobs import job_store, orchestrator, worker
from jarvis.jobs.policy import diagnose_stall
from jarvis.orchestrator.pipeline import MAX_STEPS, ROUND_END_NOTE, operation_id_for


@pytest.fixture(autouse=True)
def _isolate(scratch, monkeypatch):
    import durable_support

    reset_db()
    conversation.reset_for_tests()
    assembly.reset_for_tests()
    durable.reset_for_tests()
    durable_support.crash_effect.clear()
    # The semantic check on a finished job asks a real model; there is none here.
    monkeypatch.setattr("jarvis.jobs.worker._verify_result", lambda job, answer: None)
    register_effects()
    yield
    assert worker.join_all(timeout=20), "a job worker was still running at the end"
    assert background.join_all(timeout=20), "background work was still running at the end"
    durable.reset_for_tests()
    assembly.reset_for_tests()
    conversation.reset_for_tests()
    reset_db()


def lookups(n: int, start: int = 0) -> list[tuple]:
    return [("call", "look_up_fact", {"q": f"fact {i}"}) for i in range(start, start + n)]


def new_job(goal: str = "find the facts", **kw) -> dict:
    return job_store.create_job(title=kw.pop("title", "Facts"), goal=goal, **kw)


def run(job_id: str, **kw) -> dict:
    return run_and_wait(lambda: worker.run_job(job_id, event_bus=EventBus(), **kw))


def operations_for(job_id: str) -> list[dict]:
    rows = get_db().execute(
        "SELECT operation_id, capability, ok FROM operations WHERE operation_id LIKE ?"
        " ORDER BY completed_at", (f"{job_id}/%",)).fetchall()
    return [{"id": r[0], "capability": r[1], "ok": r[2]} for r in rows]


# --- rounds ----------------------------------------------------------------------------------

def test_work_longer_than_one_turn_carries_on_in_rounds_and_finishes():
    """A job is no longer cut off at one turn's 8 steps: it runs out of room, writes where it
    stands, and the next round carries on from exactly that transcript."""
    brain = install(Brain())
    job = new_job()
    brain.plan("job:", [lookups(MAX_STEPS), [("call", "effect_low", {"tag": "saved"}),
                                             ("say", "All done: three facts found.")]])

    result = run(job["id"])

    assert result["status"] == "done"
    stored = job_store.get_job(job["id"])
    assert stored["status"] == "done" and stored["result"] == "All done: three facts found."
    assert [(r["round"], r["outcome"], r["steps"]) for r in durable.rounds(job["id"])] == \
        [(0, "ran_out", MAX_STEPS), (1, "finished", 2)]
    assert len(brain.calls_for(f"job:{job['id']}")) == MAX_STEPS + 2
    # Round 1 continued from round 0's own transcript: the goal, every result, the note.
    first_of_round_1 = brain.calls_for(f"job:{job['id']}", 1)[0]
    texts = [m.get("text") or "" for m in first_of_round_1["messages"]]
    assert texts[0] == "find the facts"
    assert "(no tools offered) round 0." in texts
    # Each round restates the work: the working transcript keeps only its newest messages.
    assert texts[-1] == f"{durable.CONTINUE_TEXT}\n\nThe work you are doing: find the facts"
    results = [r for m in first_of_round_1["messages"] for r in m.get("toolResults") or []]
    assert len(results) == MAX_STEPS - 1
    # Round 0's last step asked for a progress note, not a final answer.
    assert ROUND_END_NOTE in brain.calls_for(f"job:{job['id']}", 0)[-1]["system"]
    assert effect_count("saved") == 1
    # One outcome note, one quiet tier-3 notice.
    assert [r["summary"] for r in job_store.get_trace(job["id"])].count("finished") == 1
    assert [o["reason"] for o in job_store.list_pending_outbox(max_tier=3)] == ["finished"]


def test_the_step_budget_is_never_overshot_and_parks_with_a_question(monkeypatch):
    monkeypatch.setitem(worker.STEP_BUDGET_BY_KIND, "generic", 11)
    brain = install(Brain())
    job = new_job()
    brain.plan("job:", [lookups(MAX_STEPS), lookups(MAX_STEPS, 10), lookups(5, 30)])

    result = run(job["id"])

    assert result == {"status": "awaiting_decision", "reason": "budget", "steps": 11}
    assert len(brain.calls_for(f"job:{job['id']}")) == 11  # not one step past it
    assert [r["steps"] for r in durable.rounds(job["id"])] == [MAX_STEPS, 3]
    assert job_store.get_job(job["id"])["status"] == "awaiting_decision"
    [ask] = [o for o in job_store.list_pending_outbox() if o["jobId"] == job["id"]]
    assert ask["reason"] == "budget" and "still not done after 11 steps" in ask["summary"]
    assert durable.get(job["id"])["status"] == "waiting"

    # "Keep going" grants another allowance from where it stands — and it finishes.
    brain.plan("job:", [lookups(MAX_STEPS), lookups(MAX_STEPS, 10), [("say", "Finished.")]])
    orchestrator.resume(job["id"], event_bus=EventBus())
    assert worker.join_all(timeout=20)
    assert job_store.get_job(job["id"])["status"] == "done"
    assert len(brain.calls_for(f"job:{job['id']}")) == 12
    assert [o for o in job_store.list_pending_outbox()
            if o["jobId"] == job["id"] and o["reason"] != "finished"] == []


def test_a_finished_job_is_not_run_again_by_a_plain_start():
    brain = install(Brain())
    job = new_job()
    brain.plan("job:", [[("say", "Done.")]])
    run(job["id"])
    assert run(job["id"])["status"] == "done"
    assert len(brain.calls_for(f"job:{job['id']}")) == 1


# --- replay-stable operation ids ---------------------------------------------------------------

def test_an_operation_id_is_what_the_call_is_not_when_it_was_made():
    seen: dict = {}
    a = operation_id_for("w/e0/r1", "send", {"to": "a", "body": "b"}, seen)
    again = operation_id_for("w/e0/r1", "send", {"body": "b", "to": "a"}, seen)
    other = operation_id_for("w/e0/r1", "send", {"to": "c", "body": "b"}, seen)
    assert a.endswith("#1") and again.endswith("#2") and a[:-2] == again[:-2]
    assert other.endswith("#1") and other[:-2] != a[:-2]
    # A replay starts counting afresh and gets exactly the same ids in the same order.
    replay: dict = {}
    assert [operation_id_for("w/e0/r1", "send", {"to": "a", "body": "b"}, replay)
            for _ in range(2)] == [a, again]
    assert operation_id_for("w/e0/r2", "send", {"to": "a", "body": "b"}, {}) != a
    assert operation_id_for("w/e1/r1", "send", {"to": "a", "body": "b"}, {}) != a


def test_a_jobs_tool_calls_are_recorded_under_its_own_scope():
    brain = install(Brain())
    job = new_job()
    brain.plan("job:", [[("call", "effect_low", {"tag": "a"}), ("call", "effect_low", {"tag": "a"}),
                         ("say", "ok")]])
    run(job["id"])
    ids = [o["id"] for o in operations_for(job["id"])]
    assert len(ids) == 2 and ids[0].endswith("#1") and ids[1].endswith("#2")
    assert all(i.startswith(f"{job['id']}/e0/r0:effect_low:") for i in ids)
    # The model asked twice with the same arguments: it really ran twice.
    assert effect_count("a") == 2


# --- approvals continue the job ------------------------------------------------------------------

@pytest.fixture
def client():
    from starlette.testclient import TestClient

    from jarvis.main import create_app

    return TestClient(create_app())


def _park_on_external(brain: Brain) -> tuple[dict, str]:
    job = new_job(goal="send the report")
    brain.plan("job:", [[("call", "effect_high", {"tag": "ext"})],
                        [("say", "Sent, and noted.")]])
    result = run(job["id"])
    assert result["status"] == "awaiting_decision"
    assert effect_count("ext") == 0
    return job, result["approvalId"]


def test_approving_runs_the_action_once_and_the_job_carries_on_by_itself(client):
    brain = install(Brain())
    job, approval_id = _park_on_external(brain)
    assert job_store.get_job(job["id"])["status"] == "awaiting_decision"
    assert durable.get(job["id"])["waiting_on"]["approval"]["id"] == approval_id

    answer = client.post(f"/api/approvals/{approval_id}", json={"decision": "allow"}).json()
    assert answer["ran"] is True and answer["result"]["ok"] is True
    assert worker.join_all(timeout=20)

    assert job_store.get_job(job["id"])["status"] == "done"
    assert job_store.get_job(job["id"])["result"] == "Sent, and noted."
    assert effect_count("ext") == 1
    # The next round was shown what really happened, not "needs your go-ahead".
    [call] = brain.calls_for(f"job:{job['id']}", 1)
    settled = [r for m in call["messages"] for r in m.get("toolResults") or []
               if r["name"] == "effect_high"]
    assert [r["result"] for r in settled] == [{"done": "ext"}]
    # Nothing left asking the person.
    assert [o for o in job_store.list_pending_outbox()
            if o["jobId"] == job["id"] and o["reason"] != "finished"] == []


def test_declining_lets_the_job_carry_on_without_it_and_it_never_runs(client):
    brain = install(Brain())
    job, approval_id = _park_on_external(brain)

    client.post(f"/api/approvals/{approval_id}", json={"decision": "deny"})
    assert worker.join_all(timeout=20)

    assert job_store.get_job(job["id"])["status"] == "done"
    assert effect_count("ext") == 0
    [call] = brain.calls_for(f"job:{job['id']}", 1)
    settled = [r for m in call["messages"] for r in m.get("toolResults") or []
               if r["name"] == "effect_high"]
    assert [r["result"] for r in settled] == [{"error": durable.DECLINED}]


def test_an_answer_to_a_different_approval_does_not_move_the_job():
    brain = install(Brain())
    job, _approval_id = _park_on_external(brain)
    assert orchestrator.answer_approval(job["id"], "apr_someone_else", allowed=True) is None
    assert worker.join_all(timeout=20)
    assert job_store.get_job(job["id"])["status"] == "awaiting_decision"
    assert len(brain.calls_for(f"job:{job['id']}")) == 1


# --- restart, cancel ----------------------------------------------------------------------------

def test_restart_forgets_the_saved_rounds_and_really_does_it_all_again(client):
    brain = install(Brain())
    job = new_job()
    brain.plan("job:", [[("call", "effect_low", {"tag": "low"}), ("say", "Done.")]])
    run(job["id"])
    assert effect_count("low") == 1

    restarted = client.post(f"/api/jobs/{job['id']}/restart").json()
    assert restarted["ok"] is True
    assert worker.join_all(timeout=20)

    assert job_store.get_job(job["id"])["status"] == "done"
    assert effect_count("low") == 2  # a new epoch: new operation ids, genuinely redone
    assert durable.get(job["id"])["epoch"] == 1
    notes = [r["summary"] for r in job_store.get_trace(job["id"]) if r["kind"] == "note"]
    assert notes == ["finished", "finished (attempt 2)"]
    finished = [o for o in outbox.for_job(job["id"]) if o["reason"] == "finished"]
    assert [o["detail"]["attempt"] for o in finished] == [1, 2]
    assert all(o["tier"] == 2 and o["detail"]["result"] == "Done." for o in finished)
    assert [r["round"] for r in durable.rounds(job["id"])] == [0]
    assert len(brain.calls_for(f"job:{job['id']}")) == 4


def test_cancel_stops_the_round_in_flight_and_no_later_round_starts():
    brain = install(Brain())
    job = new_job()
    key = (f"job:{job['id']}", 0, 2)
    brain.hold[key] = threading.Event()
    brain.reached[key] = threading.Event()
    brain.plan("job:", [lookups(MAX_STEPS), [("say", "never reached")]])

    worker.run_in_background(job["id"], event_bus=EventBus())
    assert brain.reached[key].wait(20)
    orchestrator.cancel(job["id"])
    brain.hold[key].set()
    assert worker.join_all(timeout=20)

    assert job_store.get_job(job["id"])["status"] == "cancelled"
    assert len(brain.calls_for(f"job:{job['id']}")) == 3  # the held call was the last
    assert durable.rounds(job["id"])[-1]["outcome"] == "stopped"
    assert durable.get(job["id"])["waiting_on"]["reason"] == "stopped"

    # Keep going after a cancel: it continues from where it stopped, with what it had.
    brain.plan("job:", [lookups(MAX_STEPS), [("say", "Finished after all.")]])
    orchestrator.resume(job["id"], event_bus=EventBus())
    assert worker.join_all(timeout=20)
    assert job_store.get_job(job["id"])["status"] == "done"
    assert len(brain.calls_for(f"job:{job['id']}", 0)) == 3


def test_a_cancelled_job_starts_no_new_round_even_if_it_missed_the_signal():
    brain = install(Brain())
    job = new_job()
    brain.plan("job:", [[("say", "x")]])
    job_store.update_job(job["id"], {"status": "cancelled"})
    durable.advance(job["id"], worker.KIND, job["goal"])
    assert brain.calls == []
    assert durable.rounds(job["id"])[0]["outcome"] == "stopped"


# --- isolation ------------------------------------------------------------------------------------

def test_two_jobs_at_once_never_see_each_others_work():
    brain = install(Brain())
    a, b = new_job(goal="goal A"), new_job(goal="goal B")
    brain.plan(f"job:{a['id']}", [[("call", "effect_low", {"tag": "A"}), ("say", "A done")]])
    brain.plan(f"job:{b['id']}", [[("call", "effect_low", {"tag": "B"}), ("say", "B done")]])

    worker.run_in_background(a["id"], event_bus=EventBus())
    worker.run_in_background(b["id"], event_bus=EventBus())
    assert worker.join_all(timeout=20)

    for job, mine, theirs in ((a, "A", "B"), (b, "B", "A")):
        assert job_store.get_job(job["id"])["result"] == f"{mine} done"
        for call in brain.calls_for(f"job:{job['id']}"):
            flat = json.dumps(call["messages"])
            assert f"goal {mine}" in flat and f"goal {theirs}" not in flat
        assert [o["id"].split("/")[0] for o in operations_for(job["id"])] == [job["id"]]
        names = [json.loads(r["detail"]).get("args", {}).get("tag")
                 for r in job_store.get_trace(job["id"]) if r["kind"] == "tool"
                 and r["phase"] == "intent"]
        assert names == [mine]
    assert effect_count("A") == 1 and effect_count("B") == 1


# --- recovery ---------------------------------------------------------------------------------------

def test_recovery_after_a_crash_is_idempotent_however_often_it_is_asked():
    brain = install(Brain())
    job = new_job()
    session = f"job:{job['id']}"
    brain.plan("job:", [[("call", "effect_low", {"tag": "r0"})] + lookups(MAX_STEPS - 1),
                        [("call", "effect_low", {"tag": "r1"}), ("call", "look_up_fact", {"q": "z"}),
                         ("say", "Done in the end.")]])
    brain.crash_at.add((session, 1, 1))  # round 1, after its effect ran
    run(job["id"])
    assert job_store.get_job(job["id"])["status"] == "running"  # the process "died"

    restart(brain)
    threads = [threading.Thread(target=orchestrator.recover_orphans, kwargs={"event_bus": EventBus()}),
               threading.Thread(target=orchestrator.recover_orphans, kwargs={"event_bus": EventBus()}),
               threading.Thread(target=orchestrator.supervise, kwargs={"event_bus": EventBus()})]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
        assert not t.is_alive()
    assert worker.join_all(timeout=20)

    assert job_store.get_job(job["id"])["status"] == "done"
    assert len(brain.calls_for(session, 0)) == MAX_STEPS  # round 0 never asked again
    # Round 1: the crashed call, then the replay's three calls — once, not once per recovery.
    assert len(brain.calls_for(session, 1)) == 2 + 3
    assert effect_count("r0") == 1 and effect_count("r1") == 1  # recalled, not repeated
    assert [o["reason"] for o in job_store.list_pending_outbox(max_tier=3)
            if o["jobId"] == job["id"]] == ["finished"]


def test_clearing_recorded_failures_touches_only_that_rounds_own_low_risk_calls():
    db = get_db()
    rows = [("w1/e0/r1:effect_low:aa#1", "effect_low", 0),
            ("w1/e0/r1:effect_high:bb#1", "effect_high", 0),
            ("w1/e0/r1:effect_low:cc#1", "effect_low", 1),
            ("w1/e0/r10:effect_low:dd#1", "effect_low", 0),
            ("w1/e0/r2:effect_low:ee#1", "effect_low", 0),
            ("wX1/e0/r1:effect_low:ff#1", "effect_low", 0)]
    for op, cap, ok in rows:
        db.execute("INSERT INTO operations (operation_id, capability, session_id, outcome, ok,"
                   " attempts, completed_at) VALUES (?, ?, 's', 'failed', ?, 1, 'now')",
                   (op, cap, ok))
    assert durable._clear_failures("w1/e0/r1") == 1
    left = {r[0] for r in db.execute("SELECT operation_id FROM operations").fetchall()}
    assert left == {op for op, _, _ in rows} - {"w1/e0/r1:effect_low:aa#1"}


def test_a_transient_failure_is_tried_again_when_the_round_is_replayed():
    brain = install(Brain())
    job = new_job()
    session = f"job:{job['id']}"
    attempts = {"n": 0}

    def flaky(q: str = "") -> dict:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("the network blinked")
        return {"found": q}

    from jarvis.capabilities import CapabilitySpec, Risk

    assembly.get_registry().register(CapabilitySpec(
        id="flaky_lookup", name="flaky_lookup", description="flaky", risk=Risk.LOW,
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        handler=flaky, tags=frozenset({"core"})))
    brain.plan("job:", [[("call", "flaky_lookup", {"q": "x"}), ("say", "ok")]])
    brain.crash_at.add((session, 0, 1))
    run(job["id"])
    assert operations_for(job["id"])[0]["ok"] == 0

    restart(brain)
    assembly.get_registry().register(CapabilitySpec(
        id="flaky_lookup", name="flaky_lookup", description="flaky", risk=Risk.LOW,
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        handler=flaky, tags=frozenset({"core"})))
    orchestrator.recover_orphans(event_bus=EventBus())
    assert worker.join_all(timeout=20)
    assert attempts["n"] == 2
    assert [o["ok"] for o in operations_for(job["id"])] == [1]
    assert job_store.get_job(job["id"])["status"] == "done"


# --- the trace, and loop detection on it ---------------------------------------------------------

def test_the_trace_is_written_before_and_after_each_action_with_its_operation_id():
    brain = install(Brain())
    job = new_job()
    brain.plan("job:", [[("call", "look_up_fact", {"q": "a"}), ("say", "ok")]])
    run(job["id"])
    rows = [r for r in job_store.get_trace(job["id"]) if r["kind"] == "tool"]
    assert [(r["phase"], r["effect"]) for r in rows] == [("intent", "read"), ("outcome", "read")]
    intent, outcome = (json.loads(r["detail"]) for r in rows)
    assert intent["name"] == "look_up_fact" and intent["args"] == {"q": "a"}
    assert intent["operationId"] == outcome["operationId"] == operations_for(job["id"])[0]["id"]
    assert rows[0]["seq"] < rows[1]["seq"]


def test_loop_detection_sees_a_job_repeating_itself_and_not_one_making_progress():
    brain = install(Brain())
    looping, moving = new_job(goal="loop"), new_job(goal="move")
    brain.plan(f"job:{looping['id']}", [[("call", "look_up_fact", {"q": "same"})] * 3
                                         + [("say", "x")]])
    brain.plan(f"job:{moving['id']}", [lookups(3) + [("say", "y")]])
    run(looping["id"])
    run(moving["id"])
    stall = diagnose_stall(job_store.get_trace_tail(looping["id"], 8))
    assert stall is not None and stall["cause"] == "exact_repeat"
    assert diagnose_stall(job_store.get_trace_tail(moving["id"], 8)) is None


def test_after_a_nudge_the_old_pattern_is_not_held_against_the_job():
    job = new_job()
    for _ in range(3):
        job_store.append_trace(job["id"], phase="intent", effect="read", kind="tool",
                               summary="x", detail={"name": "look", "args": {"q": 1}})
    assert diagnose_stall(job_store.get_trace_tail(job["id"], 8))["cause"] == "exact_repeat"
    job_store.append_trace(job["id"], phase="intent", effect="read", kind="nudge",
                           summary="retrying after exact_repeat", detail="x")
    assert diagnose_stall(job_store.get_trace_tail(job["id"], 8)) is None


def test_a_stalled_job_is_stopped_and_resumed_once_never_run_twice_at_once():
    """The supervisor's one retry continues the same work; a live run is stopped first."""
    brain = install(Brain())
    job = new_job()
    session = f"job:{job['id']}"
    key = (session, 0, 3)
    brain.hold[key] = threading.Event()
    brain.reached[key] = threading.Event()
    brain.plan("job:", [[("call", "look_up_fact", {"q": "same"})] * 3 + [("say", "x")],
                        [("say", "Different approach worked.")]])
    worker.run_in_background(job["id"], event_bus=EventBus())
    assert brain.reached[key].wait(20)

    actions = orchestrator.supervise(event_bus=EventBus())
    assert [a["action"] for a in actions] == ["retried"]
    brain.hold[key].set()
    assert worker.join_all(timeout=20)

    assert job_store.get_job(job["id"])["status"] == "done"
    assert job_store.get_job(job["id"])["retries"] == 1
    # The retry was told why, and continued the same transcript (round 1, not round 0 again).
    [nudged] = brain.calls_for(session, 1)
    assert "did not get anywhere" in nudged["messages"][-1]["text"]
    assert len(brain.calls_for(session, 0)) == 4


def test_a_waiting_job_is_never_answered_by_a_plain_start_or_recovery():
    """Only an explicit answer continues it: a start, a recovery, a second worker — nothing
    that is not the person (or the supervisor's retry) may move a job past its question."""
    brain = install(Brain())
    job, _approval = _park_on_external(brain)
    calls = len(brain.calls)

    # The runner itself refuses, whoever asks...
    reached = durable.advance(job["id"], worker.KIND, job["goal"])
    assert reached["state"] == "waiting" and reached["why"]["reason"] == "approval"
    # ...and so does the worker.
    assert run(job["id"])["status"] == "awaiting_decision"
    assert len(brain.calls) == calls
    assert durable.get(job["id"])["status"] == "waiting"
    assert job_store.get_job(job["id"])["status"] == "awaiting_decision"
    assert effect_count("ext") == 0


def test_a_job_row_out_of_step_with_its_durable_work_is_put_back_not_left_queued():
    """A crash between the durable record and the job's own row: recovery reads the durable
    record, runs nothing, and the job says where it really is."""
    brain = install(Brain())
    job, _approval = _park_on_external(brain)
    job_store.update_job(job["id"], {"status": "running"})  # as a crash could leave it
    calls = len(brain.calls)

    restart(brain)
    orchestrator.recover_orphans(event_bus=EventBus())
    assert worker.join_all(timeout=20)

    assert job_store.get_job(job["id"])["status"] == "awaiting_decision"
    assert len(brain.calls) == calls and effect_count("ext") == 0


def test_work_that_died_just_after_it_paused_pauses_again_without_running_anything():
    """The durable record still says running, but the saved state is already the pause:
    picking it up re-reaches the same question, runs no model call and asks nothing twice."""
    brain = install(Brain())
    job, approval_id = _park_on_external(brain)
    durable._the_store().set(job["id"], status="running")  # the crash came before this was saved
    job_store.update_job(job["id"], {"status": "running"})
    calls = len(brain.calls)

    restart(brain)
    orchestrator.recover_orphans(event_bus=EventBus())
    assert worker.join_all(timeout=20)

    assert durable.get(job["id"])["status"] == "waiting"
    assert durable.get(job["id"])["waiting_on"]["approval"]["id"] == approval_id
    assert job_store.get_job(job["id"])["status"] == "awaiting_decision"
    assert len(brain.calls) == calls and effect_count("ext") == 0
    asks = [o for o in job_store.list_pending_outbox() if o["jobId"] == job["id"]]
    assert len(asks) == 1


def test_recovery_never_hands_an_earlier_answer_to_a_later_question(client):
    """Found by a real restart of the app: work paused for the SECOND time, picked back up
    after a crash, was continued with the person's FIRST answer (LangGraph's `interrupt()`
    replays earlier answers). A wait now ends the run, and only a new answer continues it."""
    brain = install(Brain())
    job = new_job(goal="send two reports")
    brain.plan("job:", [[("call", "effect_high", {"tag": "one"})],
                        [("call", "effect_high", {"tag": "two"})],
                        [("say", "Both sent.")]])
    first = run(job["id"])["approvalId"]
    client.post(f"/api/approvals/{first}", json={"decision": "allow"})
    assert worker.join_all(timeout=20)
    second = durable.get(job["id"])["waiting_on"]["approval"]["id"]
    assert second != first and effect_count("one") == 1
    calls = len(brain.calls)

    # The crash came just before "waiting" was written down.
    durable._the_store().set(job["id"], status="running")
    job_store.update_job(job["id"], {"status": "running"})
    restart(brain)
    orchestrator.recover_orphans(event_bus=EventBus())
    assert worker.join_all(timeout=20)

    assert len(brain.calls) == calls  # nothing ran
    assert effect_count("two") == 0
    assert durable.get(job["id"])["waiting_on"]["approval"]["id"] == second
    assert job_store.get_job(job["id"])["status"] == "awaiting_decision"

    # The real answer to the second question still works.
    client.post(f"/api/approvals/{second}", json={"decision": "allow"})
    assert worker.join_all(timeout=20)
    assert job_store.get_job(job["id"])["result"] == "Both sent."
    assert effect_count("two") == 1
