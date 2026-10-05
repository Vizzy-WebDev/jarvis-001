"""After a restart: specialist runs Jarvis was waiting on, started again and delivered.

A run Jarvis (or the person) asked a specialist for, cut off by a restart, is started again
from its task as durable work, with nobody waiting; its result reaches the person the way a
late result always does — once. A run nested inside another, or inside a job, is closed off.
Asserted from the run records, the outbox, the notifications and the durable record.
"""

from __future__ import annotations

import pytest

import durable_support
from durable_support import Brain, SimulatedCrash, effect_count, install, register_effects, restart
from jarvis import assembly, background, conversation, durable, notifications
from jarvis.agents import ensure_builtins, store
from jarvis.agents import capabilities as agent_capabilities
from jarvis.agents import durable_runs
from jarvis.db import reset_for_tests as reset_db
from jarvis.heartbeat import outbox
from jarvis.jobs import worker


#: A simulated crash on a background thread ends that thread the way a dying process ends
#: everything — with an exception nothing catches. Expected here, not a warning sign.
pytestmark = pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    conversation.reset_for_tests()
    assembly.reset_for_tests()
    durable.reset_for_tests()
    durable_support.crash_effect.clear()
    agent_capabilities.closed_at_startup.clear()
    register_effects()
    ensure_builtins()
    # The specialist may use the counted test effects.
    store.update_agent("research", {"capabilityAccess": {
        "mode": "selected", "names": ["effect_low", "look_up_fact"], "connectors": []}})
    yield
    assert background.join_all(timeout=20)
    assert worker.join_all(timeout=20)
    agent_capabilities.closed_at_startup.clear()
    durable.reset_for_tests()
    assembly.reset_for_tests()
    conversation.reset_for_tests()
    reset_db()


def orphan(**kw) -> dict:
    defaults = {"agent_id": "research", "task": "find three facts about tides",
                "session_id": "agent:research:conv1", "requested_by": "jarvis",
                "conversation_id": "conv1"}
    return store.create_run(**{**defaults, **kw})


def as_a_real_kill_leaves_it(run_id: str) -> None:
    """A simulated crash still runs the run's own `finally` (which marks it failed); a real
    kill would not. Put back what a killed process leaves: the run still `running`."""
    from jarvis.db import get_db

    get_db().execute("UPDATE agent_runs SET status = 'running', error = NULL, finished_at = NULL"
                     " WHERE id = ?", (run_id,))


def startup(brain: Brain) -> dict:
    """What a real start does: the registry closes orphans off, then recovery runs."""
    restart(brain)
    found = durable_runs.recover_at_startup()
    assert background.join_all(timeout=20)
    return found


def delivered(run_id: str) -> list[dict]:
    return outbox.for_source_ref("agent", run_id)


def test_a_run_jarvis_was_waiting_on_is_started_again_and_its_result_delivered_once():
    cut_off = orphan()
    brain = Brain().plan(f"{cut_off['id']}:again", [[("call", "effect_low", {"tag": "fact"}),
                                                     ("say", "Tides follow the moon.")]])

    found = startup(brain)

    [again] = found["started_again"]
    old, new = store.get_run(cut_off["id"]), store.get_run(again)
    assert old["status"] == "failed" and f"started again ({again})" in old["error"]
    assert new["status"] == "done" and new["result"] == "Tides follow the moon."
    assert new["task"] == cut_off["task"] and new["conversationId"] == "conv1"
    assert new["requestedBy"] == "jarvis" and new["agentId"] == "research"
    [notice] = delivered(again)
    assert notice["detail"]["result"] == "Tides follow the moon."
    assert notice["detail"]["conversationId"] == "conv1"
    assert [n for n in notifications.listed() if (n.get("meta") or {}).get("runId") == again]
    assert effect_count("fact") == 1
    assert durable.get(again)["status"] == "finished"

    # The next start finds nothing more to do.
    assert startup(brain) == {"started_again": [], "resumed": []}
    assert len(store.list_runs(limit=50)) == 2
    assert len(delivered(again)) == 1


@pytest.mark.parametrize("kind", ["nested", "inside a job", "asked by a specialist"])
def test_a_run_nobody_but_another_run_was_waiting_on_is_only_closed_off(kind):
    parent = orphan()
    store.finish_run(parent["id"], status="done", result="fine")
    extra = {"nested": {"parent_run_id": parent["id"], "root_run_id": parent["id"], "depth": 2},
             "inside a job": {"job_id": "job_x", "session_id": "job:job_x",
                              "requested_by": "job"},
             "asked by a specialist": {"requested_by": "strategy"}}[kind]
    cut_off = orphan(**extra)

    found = startup(Brain())

    assert found["started_again"] == []
    closed = store.get_run(cut_off["id"])
    assert closed["status"] == "failed" and closed["error"] == "Jarvis restarted before this finished."
    assert len(store.list_runs(limit=50)) == 2


def test_a_started_again_run_cut_off_once_more_is_picked_up_where_it_was():
    cut_off = orphan()
    session = f"{cut_off['id']}:again"
    brain = Brain().plan(session, [[("call", "effect_low", {"tag": "fact"}),
                                    ("call", "look_up_fact", {"q": "moon"}),
                                    ("say", "Tides follow the moon.")]])
    brain.crash_at.add((session, 0, 2))  # after both calls ran and were recorded

    found = startup(brain)
    [again] = found["started_again"]
    assert durable.get(again)["status"] == "running"  # it died with the "process"
    as_a_real_kill_leaves_it(again)

    found = startup(brain)
    # Picked back up — not closed off as an orphan and started a second time.
    assert found == {"started_again": [], "resumed": [again]}
    assert len(store.list_runs(limit=50)) == 2
    run = store.get_run(again)
    assert run["status"] == "done" and run["result"] == "Tides follow the moon."
    assert effect_count("fact") == 1  # recalled from the record, not done again
    # The round is the unit that is saved: the cut-off round is asked again from its start
    # (3 calls, then 3 again) — but its two actions were recalled, not run again.
    assert len(brain.calls_for(session)) == 3 + 3
    assert len(delivered(again)) == 1


def test_a_started_again_run_cut_off_twice_is_reported_unfinished_once():
    cut_off = orphan()
    session = f"{cut_off['id']}:again"
    brain = Brain().plan(session, [[("call", "look_up_fact", {"q": "moon"}), ("say", "ok")]])
    brain.crash_at.add((session, 0, 1))
    [again] = startup(brain)["started_again"]
    brain.crash_at.add((session, 0, 1))
    assert startup(brain)["resumed"] == [again]
    assert durable.get(again)["status"] == "running"

    assert startup(brain)["resumed"] == [again]

    run = store.get_run(again)
    assert run["status"] == "failed" and "twice" in run["error"]
    [notice] = delivered(again)
    assert notice["detail"]["status"] == "failed"
    assert durable.get(again)["status"] == "finished"
    assert startup(brain) == {"started_again": [], "resumed": []}


def test_a_specialist_unavailable_now_is_closed_off_without_a_new_run():
    cut_off = orphan()
    store.update_agent("research", {"enabled": False})
    assert startup(Brain())["started_again"] == []
    assert store.get_run(cut_off["id"])["error"] == "Jarvis restarted before this finished."
    assert len(store.list_runs(limit=50)) == 1


def test_simulated_crash_is_not_an_ordinary_exception():
    """The crash tests depend on nothing on the way up catching it."""
    assert not issubclass(SimulatedCrash, Exception)


def test_a_crash_while_delivering_never_tells_the_person_twice(monkeypatch):
    from jarvis.agents import runner

    cut_off = orphan()
    brain = Brain().plan(f"{cut_off['id']}:again", [[("say", "Tides follow the moon.")]])
    real = runner._deliver_late
    armed = {"on": True}

    def deliver_then_die(run_id, files):
        real(run_id, files)
        if armed["on"]:
            armed["on"] = False
            raise SimulatedCrash("died right after delivering")

    monkeypatch.setattr(runner, "_deliver_late", deliver_then_die)
    [again] = startup(brain)["started_again"]
    assert durable.get(again)["status"] == "running"

    assert startup(brain)["resumed"] == [again]

    assert len(delivered(again)) == 1
    assert len([n for n in notifications.listed()
                if (n.get("meta") or {}).get("runId") == again]) == 1
    assert durable.get(again)["status"] == "finished"
