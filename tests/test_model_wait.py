"""No model able to answer is not the work failing — asserted from the record.

Before this, one provider outage stalled a job, the supervisor spent its single retry a minute
later into the same outage, and the job was "stuck" in front of the person; a scheduled run
simply failed and waited for tomorrow (8 of the 11 runs on the owner's install). Now work
nobody is watching WAITS and carries on by itself.

Real turn loop, real durable runner, real job worker and supervisor, real scheduler. Only the
model is a stand-in: `OutageBrain` fails the way the model client really does — a
`ModelUnavailable` carrying the layer's own account of the failure (`boundary.failure`).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from durable_support import Brain, effect_count, install, register_effects, restart, run_and_wait
from jarvis import assembly, background, conversation, durable
from jarvis.db import reset_for_tests as reset_db
from jarvis.events.bus import EventBus
from jarvis.heartbeat import outbox
from jarvis.jobs import job_store, orchestrator, worker
from jarvis.orchestrator.model_port import ModelUnavailable
from jarvis.orchestrator.pipeline import MAX_STEPS


@pytest.fixture(autouse=True)
def _isolate(scratch, monkeypatch):
    reset_db()
    conversation.reset_for_tests()
    assembly.reset_for_tests()
    durable.reset_for_tests()
    monkeypatch.setattr("jarvis.jobs.worker._verify_result", lambda job, answer: None)
    register_effects()
    yield
    assert worker.join_all(timeout=20), "a job worker was still running at the end"
    assert background.join_all(timeout=20), "background work was still running at the end"
    durable.reset_for_tests()
    assembly.reset_for_tests()
    conversation.reset_for_tests()
    reset_db()


def outage(words: str = "stub.test is busy right now.", **detail) -> ModelUnavailable:
    """What the model client raises when the failure is wider than this one request."""
    return ModelUnavailable(words, detail={"reason": "unavailable", "scope": "endpoint",
                                           "recovery": "after", "retryAfter": None, **detail})


def bad_request() -> ModelUnavailable:
    return ModelUnavailable("stub.test couldn't use that request.",
                            detail={"reason": "invalid_request", "scope": "request",
                                    "recovery": "none", "retryAfter": None})


class OutageBrain(Brain):
    """A model that fails its next calls with whatever is queued in `failures`."""

    def __init__(self) -> None:
        super().__init__()
        self.failures: list[Exception] = []
        self.refused = 0

    def stream(self, **kw):  # type: ignore[override]
        if self.failures:
            self.refused += 1
            raise self.failures.pop(0)
        return super().stream(**kw)


def new_job(goal: str = "find the facts") -> dict:
    return job_store.create_job(title="Facts", goal=goal)


def run(job_id: str, **kw) -> dict:
    return run_and_wait(lambda: worker.run_job(job_id, event_bus=EventBus(), **kw))


def trace_kinds(job_id: str) -> list[str]:
    return [row["kind"] for row in job_store.get_trace(job_id)]


# --- which failures are worth waiting out ------------------------------------------------------

@pytest.mark.parametrize("code,detail,expected", [
    ("no_model", {"reason": "unavailable", "scope": "endpoint"}, 0.0),
    ("no_model", {"reason": "rate_limited", "scope": "endpoint", "retryAfter": 42}, 42.0),
    ("no_model", {"reason": "billing", "scope": "connection"}, 0.0),      # a daily quota resets
    ("no_model", {"reason": "auth", "scope": "connection"}, 0.0),         # it rests, others are tried
    ("no_model", {"reason": "no_eligible_endpoint", "scope": "request", "rejected": ["resting"]}, 0.0),
    ("no_model", {"reason": "no_eligible_endpoint", "scope": "request",
                  "rejected": ["not_a_chat_model", "connection_unreachable"]}, 0.0),
    # Waiting would wait for nothing:
    ("no_model", {"reason": "no_eligible_endpoint", "scope": "request", "rejected": ["no_key"]}, None),
    ("no_model", {"reason": "no_eligible_endpoint", "scope": "request", "rejected": []}, None),
    ("no_model", {"reason": "invalid_request", "scope": "request"}, 0.0),   # tried again, briefly
    ("no_model", {"reason": "model_not_found", "scope": "endpoint"}, 0.0),
    ("no_model", {"reason": "context_too_long", "scope": "request"}, None),
    ("no_model", {"reason": "budget_exceeded", "scope": "request", "rejected": ["budget_spent"]}, None),
    ("no_model", {}, None),                  # nothing set up at all
    ("out_of_steps", {"scope": "endpoint"}, None),
    (None, {"scope": "endpoint"}, None),
])
def test_only_a_failure_that_heals_by_itself_is_waited_out(code, detail, expected):
    assert durable.model_wait(code, detail) == expected


def test_the_wait_grows_and_a_server_that_asked_for_longer_is_believed():
    waits = [durable.model_retry_in(n) for n in range(1, 8)]
    assert waits == sorted(waits) and waits[0] == 60 and waits[-1] == waits[-2] == 1800
    assert durable.model_retry_in(1, asked=600) == 600
    assert durable.model_retry_in(5, asked=10) == 1800


def test_the_layer_says_how_far_a_failure_reaches_and_what_ruled_models_out():
    """The turn loop only ever sees `ModelUnavailable.detail`; this is where it is filled in."""
    from jarvis.models import boundary, errors
    from jarvis.models.types import Rejection

    busy = boundary.failure(errors.RateLimited("x is busy.", retry_after=30), pinned_selection=False)
    assert durable.model_wait("no_model", busy.detail) == 30.0
    none_left = boundary.failure(errors.NoEligibleEndpoint(
        "Nothing can answer.", rejections=(Rejection("a/m", "resting"), Rejection("b/m", "no_key"))),
        pinned_selection=False)
    assert none_left.detail["rejected"] == ["no_key", "resting"]
    assert durable.model_wait("no_model", none_left.detail) == 0.0
    refused = boundary.failure(errors.InvalidRequest("x couldn't use that."), pinned_selection=False)
    assert durable.model_wait("no_model", refused.detail) == 0.0 and durable.model_refused(refused.detail)
    too_long = boundary.failure(errors.ContextTooLong("too long."), pinned_selection=False)
    assert durable.model_wait("no_model", too_long.detail) is None


# --- a job --------------------------------------------------------------------------------------

def test_a_job_that_meets_an_outage_waits_instead_of_stalling_and_then_finishes(monkeypatch):
    brain = install(OutageBrain())
    job = new_job()
    # The scripted model answers by round: carrying on after a wait is a new round (the work
    # is told to carry on), so what it does once a model is back is round 1's script.
    brain.plan("job:", [[], [("call", "effect_low", {"tag": "a"}), ("call", "effect_low", {"tag": "b"}),
                             ("say", "Both done.")]])
    brain.failures = [outage()]  # the very first call

    result = run(job["id"])

    # Waiting — not stalled, not stuck, nothing asked of the person, no retry spent.
    assert result["status"] == "queued" and result["reason"] == "model"
    stored = job_store.get_job(job["id"])
    assert stored["status"] == "queued" and not stored["error"] and stored["retries"] == 0
    assert stored["currentStep"].startswith("waiting for a model to be available — trying again at ")
    assert job_store.list_pending_outbox(max_tier=3) == []
    work = durable.get(job["id"])
    assert work["status"] == "waiting" and work["waiting_on"]["reason"] == "model"
    assert work["waiting_on"]["attempt"] == 1 and work["waiting_on"]["steps"] == 0
    assert "wait" in trace_kinds(job["id"]) and "note" not in trace_kinds(job["id"])

    # Not due yet: neither the supervisor's pass nor a plain start touches it.
    assert orchestrator.supervise(event_bus=EventBus()) == []
    assert run(job["id"])["status"] == "queued"
    assert brain.refused == 1 and brain.calls == []

    # Its time comes: the supervisor's own pass continues it, and it finishes.
    assert durable.wake_due(now="9999-01-01T00:00:00.000Z") == [job["id"]]
    assert worker.join_all(timeout=20)
    stored = job_store.get_job(job["id"])
    assert stored["status"] == "done" and stored["result"] == "Both done." and stored["retries"] == 0
    assert effect_count("a") == 1 and effect_count("b") == 1
    assert durable.get(job["id"])["status"] == "finished"


def test_an_outage_in_the_middle_keeps_what_was_done_and_does_not_eat_the_step_budget(monkeypatch):
    monkeypatch.setattr(durable, "MODEL_WAITS_S", (0,))
    brain = install(OutageBrain())
    job = new_job()
    brain.plan("job:", [[("call", "effect_low", {"tag": "first"})],          # then the model goes away
                        [],                                                  # still away on the first try
                        [("call", "effect_low", {"tag": "second"}), ("say", "Finished both.")]])

    def fail_after_first(original=brain.stream):
        state = {"calls": 0}

        def stream(**kw):
            state["calls"] += 1
            if state["calls"] in (2, 3):  # the 2nd call, and the first try after the wait
                brain.refused += 1
                raise outage()
            return Brain.stream(brain, **kw)
        return stream

    brain.stream = fail_after_first()  # type: ignore[method-assign]

    assert run(job["id"])["reason"] == "model"
    assert effect_count("first") == 1
    assert durable.get(job["id"])["waiting_on"]["steps"] == 1  # the failed call is not a step

    assert orchestrator.supervise(event_bus=EventBus())[0]["action"] == "continued"
    assert worker.join_all(timeout=20)
    # Still no model on that try: waiting again, the second wait in a row.
    assert durable.get(job["id"])["waiting_on"]["attempt"] == 2

    assert orchestrator.supervise(event_bus=EventBus())[0]["action"] == "continued"
    assert worker.join_all(timeout=20)
    stored = job_store.get_job(job["id"])
    assert stored["status"] == "done" and stored["result"] == "Finished both."
    assert effect_count("first") == 1 and effect_count("second") == 1  # nothing done twice
    assert stored["retries"] == 0
    assert sum(r["steps"] for r in durable.rounds(job["id"])) == 3  # three answered calls only


def test_a_request_one_model_refuses_is_tried_again_and_never_blamed_on_the_approach(monkeypatch):
    """Found by a live run under Auto: the first-ranked model's id was unknown to its provider,
    and three jobs were told twice to "take a different approach" and then declared stuck."""
    monkeypatch.setattr(durable, "MODEL_WAITS_S", (0,))
    brain = install(OutageBrain())
    job = new_job()
    brain.plan("job:", [[], [("say", "Done once a model took it.")]])
    brain.failures = [bad_request()]

    assert run(job["id"])["reason"] == "model"                 # waiting, not stalled
    assert orchestrator.supervise(event_bus=EventBus())[0]["action"] == "continued"
    assert worker.join_all(timeout=20)
    stored = job_store.get_job(job["id"])
    assert stored["status"] == "done" and not stored["attempts"] and stored["retries"] == 0


class SilentBrain(Brain):
    """Answers its first `silent` calls with no words and nothing to call."""

    def __init__(self, silent: int) -> None:
        super().__init__()
        self.silent = silent

    def stream(self, **kw):  # type: ignore[override]
        from jarvis.orchestrator.model_port import StepComplete

        if self.silent > 0:
            self.silent -= 1
            yield StepComplete(text="", model_id="brain")
            return
        yield from super().stream(**kw)


def test_a_model_that_answers_with_nothing_is_tried_again_and_never_blamed_on_the_approach(monkeypatch):
    """Found live: two empty replies used up both "try a different approach" recoveries and
    the job was declared stuck, two-thirds of the way through its work."""
    monkeypatch.setattr(durable, "MODEL_WAITS_S", (0,))
    install(SilentBrain(silent=2))                             # a step's own retry, both empty
    job = new_job()

    assert run(job["id"])["reason"] == "model"                 # waiting, not stalled
    assert orchestrator.supervise(event_bus=EventBus())[0]["action"] == "continued"
    assert worker.join_all(timeout=20)
    stored = job_store.get_job(job["id"])
    assert stored["status"] == "done" and not stored["attempts"] and stored["retries"] == 0
    assert "nudge" not in trace_kinds(job["id"])


def test_a_model_that_never_answers_stops_after_a_few_tries_and_says_so(monkeypatch):
    monkeypatch.setattr(durable, "MODEL_WAITS_S", (0,))
    install(SilentBrain(silent=10_000))
    job = new_job()

    assert run(job["id"])["reason"] == "model"
    for _ in range(durable.MODEL_REFUSAL_TRIES):
        durable.wake_due(now="9999-01-01T00:00:00.000Z")
        assert worker.join_all(timeout=20)
    stalls = [row["detail"] for row in job_store.get_trace(job["id"])
              if str(row.get("summary") or "").startswith("stalled")]
    assert stalls and "gave no answer at all" in str(stalls[0])


def test_a_request_refused_every_time_stops_after_a_few_tries_and_says_so(monkeypatch):
    monkeypatch.setattr(durable, "MODEL_WAITS_S", (0,))
    brain = install(OutageBrain())
    job = new_job()
    brain.failures = [bad_request() for _ in range(durable.MODEL_REFUSAL_TRIES + 1)]

    assert run(job["id"])["reason"] == "model"
    for _ in range(durable.MODEL_REFUSAL_TRIES):
        durable.wake_due(now="9999-01-01T00:00:00.000Z")
        assert worker.join_all(timeout=20)
    # It stopped waiting and recorded why, as a failure of the round (what the executive does
    # with that afterwards is its own business).
    stalls = [str(row.get("detail")) for row in job_store.get_trace(job["id"])
              if str(row.get("summary")).startswith("stalled: ")]
    assert stalls and "would not take the request" in stalls[0]
    assert trace_kinds(job["id"]).count("wait") == durable.MODEL_REFUSAL_TRIES


def test_waiting_ends_after_the_limit_and_says_how_long(monkeypatch):
    monkeypatch.setattr(durable, "MODEL_WAITS_S", (0,))
    monkeypatch.setattr(durable, "MODEL_WAIT_LIMIT_S", 0)
    brain = install(OutageBrain())
    job = new_job()
    brain.failures = [outage("stub.test is busy right now.")]
    result = run(job["id"])
    assert result["status"] == "stalled"
    assert "No model has been able to answer for" in result["error"]
    assert "stub.test is busy right now." in result["error"]


def test_a_restart_while_waiting_neither_loses_the_job_nor_hurries_it(monkeypatch):
    brain = install(OutageBrain())
    job = new_job()
    brain.plan("job:", [[], [("call", "effect_low", {"tag": "once"}), ("say", "Done after the restart.")]])
    brain.failures = [outage()]
    assert run(job["id"])["reason"] == "model"

    restart(brain)
    orchestrator.recover_orphans(event_bus=EventBus())   # what a real start does
    assert worker.join_all(timeout=20)
    assert job_store.get_job(job["id"])["status"] == "queued"   # still waiting: not due
    assert brain.calls == []

    # Due by the time of a later start: starting it is the retry.
    monkeypatch.setattr(durable, "model_due", lambda work, now=None: True)
    restart(brain)
    orchestrator.recover_orphans(event_bus=EventBus())
    assert worker.join_all(timeout=20)
    stored = job_store.get_job(job["id"])
    assert stored["status"] == "done" and stored["result"] == "Done after the restart."
    assert effect_count("once") == 1


def test_a_stopped_job_is_not_woken():
    brain = install(OutageBrain())
    job = new_job()
    brain.failures = [outage()]
    assert run(job["id"])["reason"] == "model"
    orchestrator.cancel(job["id"], event_bus=EventBus())
    durable.wake_due(now="9999-01-01T00:00:00.000Z")
    assert worker.join_all(timeout=20)
    assert job_store.get_job(job["id"])["status"] == "cancelled" and brain.calls == []


def test_waiting_for_a_model_is_never_mistaken_for_a_job_repeating_itself(monkeypatch):
    """Two waits in a row write two identical rows; as `note`s they would read as a job saying
    the same thing twice (`diagnose_stall`) and cost it its retry the moment it ran again."""
    from jarvis.jobs.policy import DIAGNOSE_TAIL_SIZE, diagnose_stall

    monkeypatch.setattr(durable, "MODEL_WAITS_S", (0,))
    brain = install(OutageBrain())
    job = new_job()
    brain.failures = [outage(), outage()]
    assert run(job["id"])["reason"] == "model"
    orchestrator.supervise(event_bus=EventBus())
    assert worker.join_all(timeout=20)
    assert trace_kinds(job["id"]).count("wait") == 2
    assert diagnose_stall(job_store.get_trace_tail(job["id"], DIAGNOSE_TAIL_SIZE)) is None


# --- a scheduled run ----------------------------------------------------------------------------

def _task(**action) -> dict:
    from jarvis.scheduler import task_store

    return task_store.create_task(title="Nightly", recurrence={"type": "daily", "time": "07:00"},
                                  action={"type": "prompt", "text": "do the scheduled thing", **action})


def _runs(task_id: str) -> list[dict]:
    from jarvis.scheduler import task_store

    return task_store.list_runs(task_id)


def test_a_scheduled_run_that_meets_an_outage_is_tried_again_the_same_day(monkeypatch):
    from jarvis.scheduler import engine, task_store

    monkeypatch.setattr(engine, "_verify_run", lambda task, result: None)
    monkeypatch.setattr(engine, "RETRY_WAITS_S", (0, 0))
    brain = install(OutageBrain())
    task = _task()
    brain.plan("task:", [[("call", "effect_low", {"tag": "nightly"}), ("say", "Nightly thing done.")]])
    brain.failures = [outage()]

    first = engine.run_task_now(task["id"], event_bus=EventBus())

    assert first.get("retrying") is True
    assert _runs(task["id"]) == []                       # not recorded as a failed run
    retry = task_store.get_task(task["id"])["retry"]
    assert retry["attempt"] == 1 and "busy" in retry["error"]

    ran = engine.tick(now=datetime.now() + timedelta(seconds=1), event_bus=EventBus())

    assert ran == [task["id"]]
    [recorded] = _runs(task["id"])
    assert recorded["ok"] is True and recorded["summary"] == "Nightly thing done."
    assert not task_store.get_task(task["id"]).get("retry")
    assert effect_count("nightly") == 1


def test_a_scheduled_run_gives_up_after_its_tries_and_says_so(monkeypatch):
    from jarvis.scheduler import engine, task_store

    monkeypatch.setattr(engine, "_verify_run", lambda task, result: None)
    monkeypatch.setattr(engine, "RETRY_WAITS_S", (0, 0))
    brain = install(OutageBrain())
    task = _task()
    brain.failures = [outage(), outage(), outage()]

    engine.run_task_now(task["id"], event_bus=EventBus())
    for _ in range(2):
        engine.tick(now=datetime.now() + timedelta(seconds=1), event_bus=EventBus())

    [recorded] = _runs(task["id"])
    assert recorded["ok"] is False
    assert "I tried 3 times and none could." in recorded["error"]
    assert not task_store.get_task(task["id"]).get("retry")
    assert engine.tick(now=datetime.now() + timedelta(seconds=1), event_bus=EventBus()) == []


def test_an_ordinary_failure_of_a_scheduled_run_is_recorded_at_once(monkeypatch):
    from jarvis.orchestrator.model_port import ModelUnavailable
    from jarvis.scheduler import engine, task_store

    monkeypatch.setattr(engine, "_verify_run", lambda task, result: None)
    brain = install(OutageBrain())
    task = _task()
    brain.failures = [ModelUnavailable("This month's budget for models is used up.",
                                       detail={"reason": "budget_exceeded", "scope": "request"})]
    result = engine.run_task_now(task["id"], event_bus=EventBus())
    assert not result.get("retrying")
    [recorded] = _runs(task["id"])
    assert recorded["ok"] is False and not task_store.get_task(task["id"]).get("retry")


def test_the_next_occurrence_replaces_a_pending_retry(monkeypatch):
    from jarvis.scheduler import engine, task_store

    monkeypatch.setattr(engine, "_verify_run", lambda task, result: None)
    monkeypatch.setattr(engine, "RETRY_WAITS_S", (3600,))
    brain = install(OutageBrain())
    task = _task()
    brain.plan("task:", [[("say", "Today's run.")]])
    brain.failures = [outage()]
    engine.run_task_now(task["id"], event_bus=EventBus())
    assert task_store.get_task(task["id"])["retry"]["attempt"] == 1

    due = datetime.fromisoformat(task_store.get_task(task["id"])["nextRunAt"].replace("Z", "+00:00"))
    engine.tick(now=due.astimezone().replace(tzinfo=None) + timedelta(seconds=1), event_bus=EventBus())

    assert [r["summary"] for r in _runs(task["id"])] == ["Today's run."]
    assert not task_store.get_task(task["id"]).get("retry")


def test_a_run_done_by_a_specialist_is_tried_again_too(monkeypatch):
    from jarvis.agents import ensure_builtins
    from jarvis.scheduler import engine, task_store

    monkeypatch.setattr(engine, "_verify_run", lambda task, result: None)
    monkeypatch.setattr(engine, "RETRY_WAITS_S", (0,))
    ensure_builtins()
    brain = install(OutageBrain())
    task = _task(agentId="research")
    # Every run of one task shares the specialist's session, so the retry is its round 1.
    brain.plan("agent:research", [[], [("say", "Hunted, and found two.")]])
    brain.failures = [outage()]

    assert engine.run_task_now(task["id"], event_bus=EventBus()).get("retrying") is True
    assert _runs(task["id"]) == []
    engine.tick(now=datetime.now() + timedelta(seconds=1), event_bus=EventBus())
    [recorded] = _runs(task["id"])
    assert recorded["ok"] is True and recorded["summary"] == "Hunted, and found two."
