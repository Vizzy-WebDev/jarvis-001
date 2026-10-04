"""Phase 3: follow-through and authority — asserted from what was stored and what a turn is told.

Follow-through: a finished background job's result reaches the person (a notice carrying the
whole result, a notification, and Jarvis speaking first when they are here), Jarvis knows what
it has going ("open work"), and nothing is delivered twice. Authority: a clear request is the
go-ahead for the tools that declare it, a misheard one is not, and HIGH never is.

Real parts throughout: the real turn loop, executor, outbox, notifications and prompt assembly;
only the model is scripted (`durable_support.Brain`).
"""

from __future__ import annotations

import pytest

from durable_support import Brain, effect_count, install, register_effects, run_and_wait
from jarvis import assembly, background, conversation, durable, notifications
from jarvis.capabilities.execute import ExecOutcome, execute
from jarvis.db import reset_for_tests as reset_db
from jarvis.events.bus import EventBus
from jarvis.heartbeat import outbox
from jarvis.jobs import job_store, worker
from jarvis.memory import store as memory_store
from jarvis.orchestrator import ApprovalRequired, Done, ToolRan, TurnRequest
from jarvis.orchestrator.context import RelevanceContext
from jarvis.policy import Autonomy, CallContext, Surface
from jarvis.prompt import OPEN_WORK_MAX_LINES, open_work_section


@pytest.fixture(autouse=True)
def _isolate(scratch, monkeypatch):
    import durable_support

    reset_db()
    conversation.reset_for_tests()
    assembly.reset_for_tests()
    durable.reset_for_tests()
    durable_support.crash_effect.clear()
    monkeypatch.setattr("jarvis.jobs.worker._verify_result", lambda job, answer: None)
    # Nobody is "here" unless a test says so (no UI is connected in a test anyway).
    monkeypatch.setattr("jarvis.heartbeat.speak.may_speak_now", lambda **_: False)
    register_effects()
    yield
    assert worker.join_all(timeout=20)
    assert background.join_all(timeout=20)
    durable.reset_for_tests()
    assembly.reset_for_tests()
    conversation.reset_for_tests()
    reset_db()


def finished_job(answer: str = "The answer is 42.", conversation_id: str | None = "conv-A",
                 title: str = "Find the answer") -> dict:
    brain = install(Brain())
    job = job_store.create_job(title=title, goal="find the answer",
                               conversation_id=conversation_id)
    brain.plan("job:", [[("say", answer)]])
    result = run_and_wait(lambda: worker.run_job(job["id"], event_bus=EventBus()))
    assert result["status"] == "done"
    return job_store.get_job(job["id"])


def finished_rows(job_id: str) -> list[dict]:
    return [o for o in outbox.for_job(job_id) if o["reason"] == "finished"]


def system_for(session: str, **kw) -> str:
    return RelevanceContext().assemble(session_id=session, text="hello", **kw).system


def spoken_by_jarvis() -> list[str]:
    from jarvis.session import get_active_session_id

    return [m["text"] for m in conversation.get_messages(get_active_session_id())
            if m["role"] == "assistant"]


# --- a finished result reaches the person -----------------------------------------------------

def test_a_finished_job_leaves_one_notice_carrying_its_whole_result():
    job = finished_job()
    [row] = finished_rows(job["id"])
    assert row["tier"] == 2 and row["source"] == "job" and row["deliveredAt"] is None
    assert row["detail"]["result"] == "The answer is 42."
    assert row["detail"]["conversationId"] == "conv-A" and row["detail"]["jobId"] == job["id"]
    assert row["summary"] == '"Find the answer" finished'


def test_a_finished_job_raises_one_notification():
    job = finished_job()
    [note] = [n for n in notifications.listed() if (n.get("meta") or {}).get("jobId") == job["id"]]
    assert note["kind"] == "job" and note["title"] == '"Find the answer" finished'
    assert note["body"] == "The answer is 42." and note["action"]["section"] == "jobs"


def test_the_next_turn_in_the_conversation_that_asked_is_told_the_result():
    job = finished_job()
    system = system_for("conv-A")
    [row] = finished_rows(job["id"])
    assert "The answer is 42." in system
    assert f"notice #{row['id']}" in system and "acknowledge_notice" in system


def test_another_conversation_gets_a_mention_not_the_content():
    finished_job()
    system = system_for("conv-B")
    assert "asked for in a different conversation" in system
    assert "The answer is 42." not in system


def test_a_job_with_no_conversation_is_told_wherever_the_person_next_speaks():
    finished_job(conversation_id=None)
    assert "The answer is 42." in system_for("any-chat")


def test_a_background_turn_is_never_told():
    finished_job()
    assert "The answer is 42." not in system_for("conv-A", background=True)


def test_a_long_result_is_cut_for_the_prompt_and_points_to_the_jobs_screen():
    from jarvis.prompt import LATE_RESULT_CHARS

    finished_job(answer="word " * 2000)
    system = system_for("conv-A")
    assert "the rest is on the Background Jobs screen" in system
    assert system.count("word ") < 2000 and system.count("word ") >= LATE_RESULT_CHARS // 5 - 5


def test_passing_it_on_acknowledges_it_and_it_is_not_raised_again():
    job = finished_job()
    [row] = finished_rows(job["id"])
    ctx = CallContext(session_id="conv-A", turn_id="t9", surface=Surface.TEXT,
                      autonomy=Autonomy.INTERACTIVE)
    done = execute("acknowledge_notice", {"notice_id": row["id"]}, ctx,
                   registry=assembly.get_registry())
    assert done.ok and done.value["ok"] is True
    assert finished_rows(job["id"])[0]["deliveredAt"] is not None
    assert "The answer is 42." not in system_for("conv-A")


def test_a_question_a_job_is_waiting_on_cannot_be_dismissed_as_a_notice():
    """Only a finished result is a plain notice: a pending decision is resolved by acting."""
    brain = install(Brain())
    job = job_store.create_job(title="Send it", goal="send it", conversation_id="conv-A")
    brain.plan("job:", [[("call", "effect_high", {"tag": "x"})]])
    assert run_and_wait(lambda: worker.run_job(job["id"], event_bus=EventBus()))["status"] \
        == "awaiting_decision"
    [ask] = [o for o in outbox.for_job(job["id"]) if o["reason"] == "permission"]
    ctx = CallContext(session_id="conv-A", turn_id="t9", surface=Surface.TEXT,
                      autonomy=Autonomy.INTERACTIVE)
    refused = execute("acknowledge_notice", {"notice_id": ask["id"]}, ctx,
                      registry=assembly.get_registry())
    assert refused.value["ok"] is False
    assert outbox.get(ask["id"])["deliveredAt"] is None


def test_a_finish_replayed_after_a_crash_delivers_nothing_twice():
    job = finished_job()
    stored = job_store.get_job(job["id"])
    for _ in range(3):
        worker._deliver_result(job["id"], stored, "The answer is 42.", 1, [])
    assert len(finished_rows(job["id"])) == 1
    assert len([n for n in notifications.listed()
                if (n.get("meta") or {}).get("jobId") == job["id"]]) == 1


def test_a_replay_when_the_person_has_since_arrived_still_delivers_nothing_twice(monkeypatch):
    """Whether they were here to hear it is not part of what makes two deliveries the same."""
    job = finished_job()
    stored = job_store.get_job(job["id"])
    monkeypatch.setattr("jarvis.heartbeat.speak.may_speak_now", lambda **_: True)
    worker._deliver_result(job["id"], stored, "The answer is 42.", 1, [])
    assert len(finished_rows(job["id"])) == 1
    assert spoken_by_jarvis() == []


def test_a_job_started_over_that_finishes_again_is_a_second_delivery():
    from starlette.testclient import TestClient

    from jarvis.main import create_app

    job = finished_job()
    TestClient(create_app()).post(f"/api/jobs/{job['id']}/restart")
    assert worker.join_all(timeout=20)
    rows = finished_rows(job["id"])
    assert [r["detail"]["attempt"] for r in rows] == [1, 2]
    assert rows[0]["deliveredAt"] is not None and rows[1]["deliveredAt"] is None


# --- speaking first, only when the person is here ---------------------------------------------

def test_nobody_here_means_nothing_is_said_and_the_notice_waits():
    job = finished_job()
    assert spoken_by_jarvis() == []
    [row] = finished_rows(job["id"])
    assert row["deliveredAt"] is None and row["detail"]["announced"] is False


def test_a_short_result_is_said_whole_and_the_notice_is_then_delivered(monkeypatch):
    monkeypatch.setattr("jarvis.heartbeat.speak.may_speak_now", lambda **_: True)
    job = finished_job()
    assert spoken_by_jarvis() == ['Boss, "Find the answer" is done. The answer is 42.']
    [row] = finished_rows(job["id"])
    assert row["deliveredAt"] is not None
    assert "The answer is 42." not in system_for("conv-A"), "it was already said"


def test_a_long_result_is_announced_and_the_full_result_waits_for_the_next_turn(monkeypatch):
    from jarvis.jobs.worker import SPOKEN_RESULT_CHARS

    monkeypatch.setattr("jarvis.heartbeat.speak.may_speak_now", lambda **_: True)
    long_answer = "detail " * (SPOKEN_RESULT_CHARS // 7 + 50)
    job = finished_job(answer=long_answer)
    assert spoken_by_jarvis() == ['Boss, "Find the answer" is done — I have the full result '
                                  "when you want it."]
    [row] = finished_rows(job["id"])
    assert row["deliveredAt"] is None and row["detail"]["announced"] is True
    system = system_for("conv-A")
    assert "You already told them aloud that it finished" in system and "detail detail" in system


def test_a_failure_to_speak_never_fails_the_job_or_loses_the_result(monkeypatch):
    monkeypatch.setattr("jarvis.heartbeat.speak.may_speak_now", lambda **_: True)

    def boom(*a, **k):
        raise RuntimeError("no speakers")

    monkeypatch.setattr("jarvis.heartbeat.speak.speak_now", boom)
    job = finished_job()
    assert job["status"] == "done"
    [row] = finished_rows(job["id"])
    assert row["deliveredAt"] is None  # it will be told on the next turn instead


def test_a_question_the_job_needs_answered_is_asked_aloud_when_they_are_here(monkeypatch):
    monkeypatch.setattr("jarvis.heartbeat.speak.may_speak_now", lambda **_: True)
    brain = install(Brain())
    job = job_store.create_job(title="Send it", goal="send it")
    brain.plan("job:", [[("call", "effect_high", {"tag": "x"})]])
    assert run_and_wait(lambda: worker.run_job(job["id"], event_bus=EventBus()))["status"] \
        == "awaiting_decision"
    [said] = spoken_by_jarvis()
    assert said.startswith('Boss, "Send it" needs your go-ahead')
    # The approval card is still there to answer; only the notice is spoken for.
    from jarvis.policy import approvals

    assert [a.capability for a in approvals.pending(f"job:{job['id']}")] == ["effect_high"]


def test_a_question_waits_quietly_when_they_are_not_here():
    brain = install(Brain())
    job = job_store.create_job(title="Send it", goal="send it")
    brain.plan("job:", [[("call", "effect_high", {"tag": "x"})]])
    run_and_wait(lambda: worker.run_job(job["id"], event_bus=EventBus()))
    assert spoken_by_jarvis() == []
    [ask] = [o for o in outbox.for_job(job["id"]) if o["reason"] == "permission"]
    assert ask["deliveredAt"] is None and ask["tier"] == 1


# --- "open work": Jarvis knows what it has going ----------------------------------------------

def test_nothing_open_means_no_section():
    assert open_work_section({"jobs": [], "runs": [], "watches": []}) == ""
    assert open_work_section(None) == ""
    assert "What you have going for them right now" not in system_for("s1")


def test_a_running_job_is_listed_with_where_it_is():
    job = job_store.create_job(title="Find flights", goal="g")
    job_store.update_job(job["id"], {"status": "running"})
    job_store.heartbeat(job["id"], step="using look_it_up", progress=40)
    system = system_for("s1")
    assert 'Still working: "Find flights" (using look_it_up), about 40% through' in system
    assert "never promise a follow-up that is not on it" in system


def test_a_job_waiting_on_the_person_says_what_it_needs():
    job = job_store.create_job(title="Send report", goal="g")
    job_store.update_job(job["id"], {"status": "awaiting_decision",
                                     "currentStep": "waiting on send_email"})
    assert 'Waiting on them: "Send report" — waiting on send_email' in system_for("s1")


def test_a_specialist_still_working_and_a_watch_are_listed_but_not_nested_or_job_runs():
    from jarvis.agents import ensure_builtins, store as agent_store
    from jarvis.monitor import store as monitor_store

    ensure_builtins()
    agent_store.create_run(agent_id="research", task="find three facts about tides",
                           session_id="agent:research:c", requested_by="jarvis")
    root = agent_store.create_run(agent_id="strategy", task="plan the launch",
                                  session_id="agent:strategy:c", requested_by="jarvis")
    agent_store.finish_run(root["id"], status="done", result="ok")
    agent_store.create_run(agent_id="research", task="a nested one", session_id="agent:r:c",
                           requested_by="strategy", parent_run_id="x", root_run_id="x", depth=2)
    agent_store.create_run(agent_id="research", task="a job's own run", session_id="job:j",
                           requested_by="job", job_id="job_x")
    monitor_store.create_monitor(description="the report file to appear",
                                 check={"kind": "file_exists", "path": "r.txt"},
                                 on_trigger={"type": "notify", "text": "it is there"})
    system = system_for("s1")
    assert "A specialist is still working: Research & Intelligence — find three facts" in system
    assert "Watching for: the report file to appear" in system
    assert "plan the launch" not in system and "a nested one" not in system
    assert "a job's own run" not in system


def test_a_background_turn_is_not_given_the_section():
    job = job_store.create_job(title="Find flights", goal="g")
    job_store.update_job(job["id"], {"status": "running"})
    assert "Still working" not in system_for("s1", background=True)


def test_the_section_is_capped_so_it_cannot_crowd_out_the_conversation():
    jobs = [{"title": f"job {i}", "status": "running", "currentStep": "x" * 400}
            for i in range(OPEN_WORK_MAX_LINES + 5)]
    section = open_work_section({"jobs": jobs})
    lines = [line for line in section.splitlines() if line.startswith("- ")]
    assert len(lines) == OPEN_WORK_MAX_LINES + 1 and "5 more" in lines[-1]
    assert all(len(line) < 200 for line in lines)


def test_a_failure_reading_what_is_open_never_fails_the_turn(monkeypatch):
    def boom():
        raise RuntimeError("database locked")

    monkeypatch.setattr("jarvis.jobs.job_store.list_active_jobs", boom)
    assert "Still working" not in system_for("s1")


# --- authority: a clear request is the go-ahead ------------------------------------------------

def chat_turn(brain: Brain, steps: list, *, low_confidence: bool = False, session: str = "chat-1",
              autonomy: Autonomy = Autonomy.INTERACTIVE) -> list:
    brain.plan(session, [steps])
    request = TurnRequest(text="please do it", session_id=session, surface=Surface.TEXT,
                          autonomy=autonomy, low_confidence=low_confidence)
    return list(assembly.get_orchestrator().run_turn(request))


def test_a_clear_request_to_remember_runs_with_no_card_through_the_real_turn_loop():
    events = chat_turn(install(Brain()), [("call", "remember_about_me", {"text": "Drinks tea."}),
                                          ("say", "Noted.")])
    assert not [e for e in events if isinstance(e, ApprovalRequired)]
    [ran] = [e for e in events if isinstance(e, ToolRan)]
    assert ran.ok and ran.outcome is ExecOutcome.COMPLETED
    assert [m["text"] for m in memory_store.list_memories()] == ["Drinks tea."]
    assert any(isinstance(e, Done) for e in events)


def test_the_same_request_misheard_still_asks_first():
    events = chat_turn(install(Brain()), [("call", "remember_about_me", {"text": "Drinks tea."})],
                       low_confidence=True)
    assert [e.capability for e in events if isinstance(e, ApprovalRequired)] \
        == ["remember_about_me"]
    assert memory_store.list_memories() == []


@pytest.mark.parametrize("tool,args", [
    ("allow_folder", {"path": "/tmp/anything"}),
    ("create_skill", {"name": "x-y", "description": "d", "instructions": "i"}),
    ("effect_high", {"tag": "never"}),
])
def test_what_the_owner_said_keeps_asking_still_asks_even_when_requested(tool, args):
    events = chat_turn(install(Brain()), [("call", tool, args)])
    assert [e.capability for e in events if isinstance(e, ApprovalRequired)] == [tool]
    assert effect_count("never") == 0


def test_an_unflagged_medium_tool_still_asks():
    events = chat_turn(install(Brain()), [("call", "effect_external", {"tag": "never"})])
    assert [e.capability for e in events if isinstance(e, ApprovalRequired)] == ["effect_external"]
    assert effect_count("never") == 0


def test_scheduling_on_request_just_schedules():
    from jarvis.scheduler import task_store

    events = chat_turn(install(Brain()), [("call", "schedule_task", {
        "title": "Plants", "when": "daily", "time": "09:00", "action": "reminder",
        "text": "water the plants"}), ("say", "Scheduled.")])
    assert not [e for e in events if isinstance(e, ApprovalRequired)]
    [ran] = [e for e in events if isinstance(e, ToolRan)]
    assert ran.capability == "schedule_task" and ran.ok
    assert [t["title"] for t in task_store.list_tasks()] == ["Plants"]


# --- authority inside a job -------------------------------------------------------------------

def test_a_job_the_person_started_takes_a_medium_step_without_parking():
    brain = install(Brain())
    job = job_store.create_job(title="Do it", goal="do the thing")
    brain.plan("job:", [[("call", "effect_external", {"tag": "step"}), ("say", "Done it.")]])
    result = run_and_wait(lambda: worker.run_job(job["id"], event_bus=EventBus()))
    assert result["status"] == "done"
    assert effect_count("step") == 1
    assert [o for o in outbox.for_job(job["id"]) if o["reason"] == "permission"] == []


def test_a_job_still_parks_for_a_high_risk_step_and_it_does_not_run():
    brain = install(Brain())
    job = job_store.create_job(title="Do it", goal="do the thing")
    brain.plan("job:", [[("call", "effect_high", {"tag": "step"})]])
    result = run_and_wait(lambda: worker.run_job(job["id"], event_bus=EventBus()))
    assert result["status"] == "awaiting_decision" and effect_count("step") == 0


@pytest.mark.parametrize("when", ["before", "after"])
def test_a_job_cut_off_inside_a_medium_step_is_never_repeated_unasked(when):
    """Now that a job takes MEDIUM steps on its own, the safety net from durable work carries
    real weight: if the process dies mid-action, nobody retries it behind the person's back."""
    import durable_support
    from jarvis.jobs import orchestrator

    brain = install(Brain())
    job = job_store.create_job(title="Do it", goal="do the thing")
    brain.plan("job:", [[("call", "effect_external", {"tag": "step"}), ("say", "Done it.")]])
    durable_support.crash_effect["step"] = when
    crashed = run_and_wait(lambda: worker.run_job(job["id"], event_bus=EventBus()))
    assert isinstance(crashed, durable_support.SimulatedCrash)

    durable_support.restart(brain)
    [verdict] = orchestrator.recover_orphans(event_bus=EventBus())
    assert worker.join_all(timeout=20)

    assert verdict["recovery"] == "unrecoverable"
    assert job_store.get_job(job["id"])["status"] == "awaiting_decision"
    assert effect_count("step") == (1 if when == "after" else 0)  # never a second time
    [ask] = [o for o in job_store.list_pending_outbox() if o["jobId"] == job["id"]]
    assert ask["reason"] == "crashed" and "effect_external" in ask["summary"]
