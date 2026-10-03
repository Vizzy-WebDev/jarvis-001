"""A crash at every point a background job can die, then a restart — and what must hold.

Each case runs a real two-round job, kills it at one point (`SimulatedCrash`, which nothing
on the way up catches — the process "dies" there), throws away what a dead process loses
(`durable_support.restart`), runs startup recovery, and then asserts the invariants together:

1. every finished round's model calls are NOT repeated (the model's own call log);
2. every action that finished ran exactly once (the effect counter file) — and a LOW-risk
   action cut off between doing it and recording it is the one thing allowed to run again;
3. an action reaching outside Jarvis that may or may not have happened is never repeated
   unasked — the work waits for a person, naming that action;
4. the work always ends `done` or waiting on a person with a reason — never left `running`,
   never silently dropped;
5. what the finish records (the result, its trace note, its notice, the run history) exists
   exactly once.

Plus the same process killed for real: SIGKILL across two separate Python processes.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import durable_support
from durable_support import (
    Brain, SimulatedCrash, effect_count, install, register_effects, restart, run_and_wait,
)
from jarvis import assembly, background, conversation, durable
from jarvis.db import reset_for_tests as reset_db
from jarvis.events.bus import EventBus
from jarvis.jobs import job_store, orchestrator, worker
from jarvis.orchestrator.pipeline import MAX_STEPS


#: A simulated crash on a background thread ends that thread the way a dying process ends
#: everything — with an exception nothing catches. Expected here, not a warning sign.
pytestmark = pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")


@pytest.fixture(autouse=True)
def _isolate(scratch, monkeypatch):
    reset_db()
    conversation.reset_for_tests()
    assembly.reset_for_tests()
    durable.reset_for_tests()
    durable_support.crash_effect.clear()
    monkeypatch.setattr("jarvis.jobs.worker._verify_result", lambda job, answer: None)
    register_effects()
    yield
    assert worker.join_all(timeout=20)
    assert background.join_all(timeout=20)
    durable.reset_for_tests()
    assembly.reset_for_tests()
    conversation.reset_for_tests()
    reset_db()


def lookups(n: int, start: int = 0) -> list[tuple]:
    return [("call", "look_up_fact", {"q": f"fact {i}"}) for i in range(start, start + n)]


SCRIPT = [
    [("call", "effect_low", {"tag": "r0"})] + lookups(MAX_STEPS - 2),          # 8 steps, ran out
    [("call", "effect_low", {"tag": "r1"}), ("call", "look_up_fact", {"q": "z"}),
     ("say", "Finished: both rounds done.")],
]


def _crash_before_round_0(brain, session, monkeypatch):
    brain.crash_at.add((session, 0, 0))


def _crash_mid_round_before_an_action_ran(brain, session, monkeypatch):
    durable_support.crash_effect["r1"] = "before"


def _crash_after_an_action_before_its_result_was_recorded(brain, session, monkeypatch):
    durable_support.crash_effect["r1"] = "after"


def _crash_after_a_round_finished(brain, session, monkeypatch):
    brain.crash_at.add((session, 1, 0))


def _crash_mid_round_after_an_action_was_recorded(brain, session, monkeypatch):
    brain.crash_at.add((session, 1, 1))


def _crash_during_the_finish(brain, session, monkeypatch):
    once = {"done": False}

    def verify(job, answer):
        if not once["done"]:
            once["done"] = True
            raise SimulatedCrash("crash during the finish")
        return None

    monkeypatch.setattr("jarvis.jobs.worker._verify_result", verify)


# (setup, round-0 model calls in total, round-1 model calls in total, r0 runs, r1 runs)
CASES = {
    "before round 0": (_crash_before_round_0, 1 + MAX_STEPS, 3, 1, 1),
    "mid-round, before an action ran": (_crash_mid_round_before_an_action_ran, MAX_STEPS,
                                        1 + 3, 1, 1),
    # LOW risk: cut off between doing it and recording it, it is allowed to run again.
    "after a low-risk action, before its result was recorded":
        (_crash_after_an_action_before_its_result_was_recorded, MAX_STEPS, 1 + 3, 1, 2),
    "after a round finished": (_crash_after_a_round_finished, MAX_STEPS, 1 + 3, 1, 1),
    "mid-round, after an action was recorded": (_crash_mid_round_after_an_action_was_recorded,
                                                MAX_STEPS, 2 + 3, 1, 1),
    "during the finish": (_crash_during_the_finish, MAX_STEPS, 3, 1, 1),
}


@pytest.mark.parametrize("case", list(CASES))
def test_a_job_crashed_anywhere_recovers_without_repeating_finished_work(case, monkeypatch):
    setup, round0_calls, round1_calls, r0_runs, r1_runs = CASES[case]
    brain = install(Brain())
    job = job_store.create_job(title="Two rounds", goal="do two rounds of work")
    session = f"job:{job['id']}"
    brain.plan("job:", SCRIPT)
    setup(brain, session, monkeypatch)

    crashed = run_and_wait(lambda: worker.run_job(job["id"], event_bus=EventBus()))
    assert isinstance(crashed, SimulatedCrash), f"the crash point was never reached: {crashed}"
    assert job_store.get_job(job["id"])["status"] == "running"

    restart(brain)
    recovered = orchestrator.recover_orphans(event_bus=EventBus())
    assert recovered == [{"job": job["id"], "recovery": "resumable"}]
    assert worker.join_all(timeout=20)

    # 1. finished rounds were not asked again
    assert len(brain.calls_for(session, 0)) == round0_calls
    assert len(brain.calls_for(session, 1)) == round1_calls
    # 2. finished actions ran exactly once
    assert effect_count("r0") == r0_runs
    assert effect_count("r1") == r1_runs
    # 4. it ended, and says so
    stored = job_store.get_job(job["id"])
    assert stored["status"] == "done" and stored["result"] == "Finished: both rounds done."
    assert durable.get(job["id"])["status"] == "finished"
    assert [(r["round"], r["outcome"]) for r in durable.rounds(job["id"])] == \
        [(0, "ran_out"), (1, "finished")]
    # 5. the finish is on record exactly once
    assert [r["summary"] for r in job_store.get_trace(job["id"])].count("finished") == 1
    finished = [o for o in job_store.list_pending_outbox(max_tier=3)
                if o["jobId"] == job["id"] and o["reason"] == "finished"]
    assert len(finished) == 1
    # Recovery a second time (another restart) finds nothing to do.
    restart(brain)
    assert orchestrator.recover_orphans(event_bus=EventBus()) == []
    assert worker.join_all(timeout=20)
    assert len(brain.calls_for(session)) == round0_calls + round1_calls


def test_a_crash_while_parking_still_reaches_the_person_and_their_answer_still_counts(monkeypatch):
    from starlette.testclient import TestClient

    from jarvis.main import create_app

    brain = install(Brain())
    job = job_store.create_job(title="Report", goal="send the report")
    brain.plan("job:", [[("call", "effect_high", {"tag": "ext"})], [("say", "Sent.")]])
    real_park = worker._park_for_approval
    armed = {"on": True}

    def park_then_die(job_id, approval):
        real_park(job_id, approval)
        if armed["on"]:
            armed["on"] = False
            raise SimulatedCrash("died right after parking")

    monkeypatch.setattr(worker, "_park_for_approval", park_then_die)
    assert isinstance(run_and_wait(lambda: worker.run_job(job["id"], event_bus=EventBus())),
                      SimulatedCrash)
    assert job_store.get_job(job["id"])["status"] == "awaiting_decision"
    assert durable.get(job["id"])["status"] == "running"

    restart(brain)
    assert orchestrator.recover_orphans(event_bus=EventBus()) == []  # it waits on a person
    [ask] = [o for o in job_store.list_pending_outbox() if o["jobId"] == job["id"]]
    approval_id = ask["detail"]["approvalId"]

    client = TestClient(create_app())
    assert client.post(f"/api/approvals/{approval_id}", json={"decision": "allow"}).json()["ran"]
    assert worker.join_all(timeout=20)

    assert job_store.get_job(job["id"])["status"] == "done"
    assert effect_count("ext") == 1
    asks = [o for o in job_store.list_pending_outbox(max_tier=3) if o["jobId"] == job["id"]]
    assert [o["reason"] for o in asks] == ["finished"]
    decisions = [r for r in job_store.get_trace(job["id"]) if r["kind"] == "decision"]
    assert len(decisions) == 1
    assert len(brain.calls_for(f"job:{job['id']}", 0)) == 1  # round 0 never asked again


def test_an_open_external_action_in_a_jobs_record_parks_it_naming_that_action():
    """The durable runner's own write-ahead record, read by job recovery: a job that died in
    the middle of an outside action is not continued — the person is asked to check it."""
    job = job_store.create_job(title="Report", goal="send it", status="running")
    durable._the_store().insert(job["id"], "job", "send it", 30, None)
    durable._the_store().mark_started(job["id"], 0, f"{job['id']}/e0/r0:effect_external:ab#1",
                                      "effect_external", True)

    [verdict] = orchestrator.recover_orphans(event_bus=EventBus())

    assert verdict == {"job": job["id"], "recovery": "unrecoverable"}
    stored = job_store.get_job(job["id"])
    assert stored["status"] == "awaiting_decision" and stored["currentStep"] == "check effect_external"
    [ask] = [o for o in job_store.list_pending_outbox() if o["jobId"] == job["id"]]
    assert "effect_external" in ask["summary"] and "can't tell whether it happened" in ask["summary"]


# --- scheduled runs: where an outside action really runs unattended --------------------------

def _prompt_task(text: str = "do the scheduled thing") -> dict:
    from jarvis.scheduler import task_store

    return task_store.create_task(title="Nightly", recurrence={"type": "daily", "time": "07:00"},
                                  action={"type": "prompt", "text": text})


def _runs(task_id: str) -> list[dict]:
    from jarvis.scheduler import task_store

    return task_store.list_runs(task_id)


def _startup_recovery() -> None:
    from jarvis.scheduler import durable_runs

    durable_runs.recover_interrupted()
    assert background.join_all(timeout=20)


@pytest.mark.parametrize("when", ["before", "after"])
def test_a_scheduled_run_cut_off_inside_an_outside_action_never_repeats_it(when):
    from jarvis.scheduler import engine

    brain = install(Brain())
    task = _prompt_task()
    brain.plan("task:", [[("call", "effect_external", {"tag": "ext"}), ("say", "Done.")]])
    durable_support.crash_effect["ext"] = when
    assert isinstance(run_and_wait(lambda: engine.run_task_now(task["id"])), SimulatedCrash)
    assert effect_count("ext") == (1 if when == "after" else 0)

    restart(brain)
    _startup_recovery()

    assert effect_count("ext") == (1 if when == "after" else 0)  # never a second time
    [run] = _runs(task["id"])
    assert run["ok"] is False and "may or may not have happened" in run["error"]
    assert "effect_external" in run["error"]
    _startup_recovery()  # the next start leaves it alone
    assert len(_runs(task["id"])) == 1
    assert len(brain.calls) == 1


def test_a_scheduled_run_cut_off_after_its_action_finished_resumes_once_and_says_so():
    from jarvis.scheduler import engine

    brain = install(Brain())
    task = _prompt_task()
    brain.plan("task:", [[("call", "effect_external", {"tag": "ext"}), ("say", "All done.")]])
    brain.crash_at.add(("task:", 0, 1))
    brain.crash_at = {(s, r, st) for (s, r, st) in brain.crash_at}

    # The session id is made per run, so arm the crash once the run has started.
    original_stream = brain.stream

    def stream(**kw):
        if kw["session_id"].startswith("task:") and ("task:", 0, 1) in brain.crash_at:
            from durable_support import position

            if position(kw["messages"]) == (0, 1):
                brain.crash_at.discard(("task:", 0, 1))
                raise SimulatedCrash("died after the action")
        return original_stream(**kw)

    brain.stream = stream  # type: ignore[method-assign]
    assert isinstance(run_and_wait(lambda: engine.run_task_now(task["id"])), SimulatedCrash)
    assert effect_count("ext") == 1

    restart(brain)
    _startup_recovery()

    assert effect_count("ext") == 1  # recalled from the record, not done again
    [run] = _runs(task["id"])
    assert run["ok"] is True and run["summary"] == "All done."
    assert run["resumedAfterRestart"] is True and run["note"] == "Resumed after Jarvis restarted."
    _startup_recovery()
    assert len(_runs(task["id"])) == 1


def test_a_scheduled_run_interrupted_twice_is_recorded_and_not_tried_a_third_time():
    from jarvis.scheduler import engine

    brain = install(Brain())
    task = _prompt_task()
    brain.plan("task:", [[("call", "look_up_fact", {"q": "a"}), ("say", "ok")]])
    deaths = {"left": 2}
    original_stream = brain.stream

    def stream(**kw):
        from durable_support import position

        if position(kw["messages"]) == (0, 1) and deaths["left"]:
            deaths["left"] -= 1
            raise SimulatedCrash("died again")
        return original_stream(**kw)

    brain.stream = stream  # type: ignore[method-assign]
    assert isinstance(run_and_wait(lambda: engine.run_task_now(task["id"])), SimulatedCrash)
    restart(brain)
    from jarvis.scheduler import durable_runs

    [work] = durable.with_status(durable_runs.KIND, "running")
    assert isinstance(run_and_wait(lambda: durable_runs._recover(work["id"])), SimulatedCrash)
    restart(brain)
    _startup_recovery()

    [run] = _runs(task["id"])
    assert run["ok"] is False and "twice" in run["error"]
    assert durable.get(work["id"])["status"] == "finished"
    _startup_recovery()
    assert len(_runs(task["id"])) == 1


def test_a_scheduled_run_that_died_while_being_recorded_is_recorded_once(monkeypatch):
    from jarvis.scheduler import engine

    brain = install(Brain())
    task = _prompt_task()
    brain.plan("task:", [[("call", "look_up_fact", {"q": "a"}), ("say", "Recorded once.")]])
    armed = {"on": True}

    def die_once(run):
        if armed["on"]:
            armed["on"] = False
            raise SimulatedCrash("died after the run was written down")
        return False

    monkeypatch.setattr(engine, "_should_notify", die_once)
    assert isinstance(run_and_wait(lambda: engine.run_task_now(task["id"])), SimulatedCrash)
    assert len(_runs(task["id"])) == 1

    restart(brain)
    _startup_recovery()

    [run] = _runs(task["id"])
    assert run["ok"] is True and run["summary"] == "Recorded once."
    assert len(brain.calls) == 2  # the finish was replayed; the round was not


# --- a real process, really killed ------------------------------------------------------------

CHILD = Path(__file__).with_name("durable_kill_child.py")


def _child(mode: str, scratch_dir: Path, log: Path) -> subprocess.Popen:
    env = dict(os.environ)
    assert env.get("JARVIS_DATA_DIR"), "the child must never reach the real data dir"
    here = Path(__file__).resolve().parent
    env["PYTHONPATH"] = os.pathsep.join([str(here.parent), str(here), env.get("PYTHONPATH", "")])
    return subprocess.Popen([sys.executable, str(CHILD), mode, str(log)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def _calls(log: Path) -> list[tuple[str, int, int]]:
    if not log.exists():
        return []
    return [tuple(json.loads(line)) for line in log.read_text().splitlines() if line.strip()]


def test_a_job_killed_for_real_mid_round_resumes_in_a_new_process_without_repeating(scratch):
    from jarvis.store import data_dir

    log = data_dir() / "model_calls.log"
    first = _child("first", data_dir(), log)
    deadline = time.monotonic() + 60
    while ("first", 2, 0) not in _calls(log):
        assert first.poll() is None, first.stdout.read() if first.stdout else ""
        assert time.monotonic() < deadline, "the first process never reached round 2"
        time.sleep(0.05)
    os.kill(first.pid, signal.SIGKILL)
    first.wait(10)
    assert first.returncode == -signal.SIGKILL

    before = _calls(log)
    assert sorted({(r, s) for _, r, s in before}) == \
        [(0, s) for s in range(MAX_STEPS)] + [(1, s) for s in range(MAX_STEPS)] + [(2, 0)]

    second = _child("second", data_dir(), log)
    out, _ = second.communicate(timeout=120)
    assert second.returncode == 0, out

    after = [c for c in _calls(log) if c[0] == "second"]
    # Rounds 0 and 1 were saved: the new process asked only round 2 (again, from its start).
    assert {r for _, r, _ in after} == {2}
    assert [s for _, _, s in after] == [0, 1]
    effects = (data_dir() / "effects.log").read_text().splitlines()
    assert sorted(effects) == ["a", "b", "c"]  # each exactly once, across both processes
    reset_db()
    [job] = job_store.list_jobs(status="done")
    assert job["result"] == "All three rounds done."
