"""The check of a finished job (`jobs/checking.py`) and the gate that keeps an outward action
from being done twice by accident (`objective.refuse_redo`).

Four things are pinned here, each from real jobs run through the real worker, turn loop,
executor and policy — only the model (a scripted stand-in) and the judge's answer are supplied:

- **the evidence** is the record of what ran, never the worker's account of it;
- **partial completion** is delivered as partial, saying what is and is not done;
- **recovery** — a result sent back is put right, not done over, and survives a crash;
- **duplicates** — an outward action that succeeded is not repeated by accident, in any of
  the ways it has really happened, while a repeat that is asked for still runs.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from durable_support import (Brain, SimulatedCrash, effect_count, install, register_effects, restart,
                             run_and_wait)
from jarvis import assembly, background, conversation, durable
from jarvis.db import reset_for_tests as reset_db
from jarvis.events.bus import EventBus
from jarvis.jobs import checking, job_store, objective, orchestrator, worker
from jarvis.orchestrator.model_port import StepComplete, ToolCall


@pytest.fixture(autouse=True)
def _isolate(scratch):
    import durable_support

    reset_db()
    conversation.reset_for_tests()
    assembly.reset_for_tests()
    durable.reset_for_tests()
    durable_support.crash_effect.clear()
    register_effects()
    yield
    assert worker.join_all(timeout=20), "a job worker was still running at the end"
    assert background.join_all(timeout=20)
    durable.reset_for_tests()
    assembly.reset_for_tests()
    conversation.reset_for_tests()
    reset_db()


class Judge:
    """Stands in for the model that judges. `says` is one answer per check, in order: a list
    of verdicts (one per line of done), an Exception to raise, or raw data to return as-is.
    Every prompt it was shown is kept."""

    def __init__(self, monkeypatch, *says):
        self.says, self.prompts, self.systems = list(says), [], []
        monkeypatch.setattr("jarvis.ai.ask", self)

    def __call__(self, prompt, **kw):
        self.prompts.append(prompt)
        self.systems.append(kw.get("system", ""))
        assert kw["task_class"] == "judge" and kw["data_class"] == "personal" and kw["want_json"]
        answer = self.says.pop(0) if self.says else ["met"] * 20
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, list):
            answer = {"lines": [{"n": n, "verdict": v, "why": f"because {v}"} for n, v in enumerate(answer, 1)]}
        return SimpleNamespace(data=answer)


def new_job(*done_when: str, goal: str = "File the March report and tell Sam.") -> dict:
    return job_store.create_job(title="March", goal=goal,
                                brief=objective.make_brief(outcome=goal, done_when=list(done_when)))


def run(job_id: str, **kw) -> dict:
    return run_and_wait(lambda: worker.run_job(job_id, event_bus=EventBus(), **kw))


def carry_on() -> list[dict]:
    actions = orchestrator.supervise(event_bus=EventBus())
    assert worker.join_all(timeout=20)
    return actions


def outbox_for(job_id: str) -> list[dict]:
    return [o for o in job_store.list_pending_outbox(max_tier=3) if o["jobId"] == job_id]


def told(brain: Brain, job_id: str, round_no: int) -> str:
    return brain.calls_for(f"job:{job_id}", round_no)[0]["messages"][-1]["text"]


def results_seen(brain: Brain, job_id: str) -> list[dict]:
    """Every tool result that came back to the worker, in order."""
    out, seen = [], set()
    for call in brain.calls_for(f"job:{job_id}"):
        for message in call["messages"]:
            for result in message.get("toolResults") or []:
                if result["id"] not in seen:
                    seen.add(result["id"])
                    out.append(result)
    return out


FILE = ("call", "effect_external", {"tag": "report"})
TELL = ("call", "effect_external", {"tag": "told-sam"})


# --- the evidence ---------------------------------------------------------------------------------

def test_the_record_is_what_ran_not_what_the_worker_says(monkeypatch):
    judge = Judge(monkeypatch, ["met", "not_met"], ["met", "met"])
    brain = install(Brain())
    job = new_job("the March report is filed", "Sam has been told")
    brain.plan("job:", [[FILE, ("call", "look_up_fact", {"q": "sam"}),
                         ("say", "Filed the report and told Sam.")],        # it never told Sam
                        [TELL, ("say", "Filed the report and told Sam.")]])
    run(job["id"])

    record = checking.evidence(job["id"])
    assert [(a["name"], a["args"], a["outward"], a["result"]) for a in record["actions"]] == [
        ("effect_external", {"tag": "report"}, True, "ok"),
        ("look_up_fact", {"q": "sam"}, False, "ok")]
    shown = judge.prompts[0]
    # The judge is given the lines of done, the record and the report — and which to believe.
    assert "1. the March report is filed" in shown and "2. Sam has been told" in shown
    assert 'effect_external({"tag": "report"}) → SUCCEEDED' in shown
    assert "told-sam" not in shown.split("THE WORKER'S REPORT")[0]     # not in the record
    assert "look_up_fact ×1" in shown and "Filed the report and told Sam." in shown
    assert "Trust the record over the report" in judge.systems[0]


def test_a_failed_action_never_put_right_keeps_a_result_from_being_called_verified(monkeypatch):
    """Whatever the judge says: this needs no model."""
    Judge(monkeypatch, ["met"])                                         # the judge is fooled
    registry = assembly.get_registry()
    from jarvis.capabilities import CapabilitySpec, Risk

    def refuse(**_):
        raise RuntimeError("the records office is closed")

    registry.register(CapabilitySpec(id="send_fax", name="send_fax", description="Send a fax (test).",
                                     input_schema={"type": "object", "properties": {"to": {"type": "string"}}},
                                     handler=refuse, risk=Risk.MEDIUM, tags=frozenset({"core"})))
    brain = install(Brain())
    job = new_job("the fax is sent")
    brain.plan("job:", [[("call", "send_fax", {"to": "Sam"}), ("say", "Fax sent to Sam.")]])
    run(job["id"])

    stored = job_store.get_job(job["id"])
    check = objective.ledger_of(stored)["check"]
    assert stored["status"] == "done" and check["verdict"] == "partly"
    [fact] = check["facts"]
    assert fact["kind"] == "failed" and "send_fax" in fact["text"] and "records office is closed" in fact["text"]
    assert "failed and was never done successfully" in checking.label(check)


def test_findings_need_no_model():
    def action(name, tag, result, outward=True):
        return {"name": name, "args": {"tag": tag}, "outward": outward, "result": result, "error": None}

    record = {"unsure": [action("pay", "invoice-9", "ok")], "actions": [
        action("send", "a", "failed"), action("send", "a", "ok"),             # put right: no finding
        action("send", "b", "failed"),                                         # never put right
        action("file", "c", "ok"), action("file", "c", "ok"),                  # done twice
        action("read", "d", "failed", outward=False),                          # a read: nobody's business
        action("send", "e", "not_run")]}                                       # refused: not a failure
    found = checking.findings(record)
    assert sorted((f["kind"], f["text"].split("(")[0]) for f in found) == [
        ("failed", "send"), ("twice", "file"), ("unsure", "pay")]
    assert "done 2 times" in next(f["text"] for f in found if f["kind"] == "twice")


@pytest.mark.parametrize("answer", [
    RuntimeError("no model can answer"), {"lines": "yes"}, {"matches": True}, "fine", {"lines": []},
])
def test_a_check_that_could_not_be_made_is_never_a_pass(monkeypatch, answer):
    Judge(monkeypatch, answer)
    brain = install(Brain())
    job = new_job("the March report is filed")
    brain.plan("job:", [[FILE, ("say", "Filed.")]])
    run(job["id"])

    stored = job_store.get_job(job["id"])
    assert stored["status"] == "done"
    assert objective.ledger_of(stored)["check"]["verdict"] == "unchecked"
    [notice] = outbox_for(job["id"])
    assert notice["detail"]["check"] == "unchecked" and notice["detail"]["checked"].startswith("Not checked")
    assert notice["detail"]["result"] == "Filed."


def test_a_line_the_judge_left_out_is_unknown_not_met(monkeypatch):
    Judge(monkeypatch, {"lines": [{"n": 1, "verdict": "met", "why": "filed"},
                                  {"n": 7, "verdict": "met"}, {"n": "x"}, "junk"]})
    brain = install(Brain())
    job = new_job("the March report is filed", "Sam has been told")
    brain.plan("job:", [[FILE, ("say", "Filed and told.")]])
    run(job["id"])
    check = objective.ledger_of(job_store.get_job(job["id"]))["check"]
    assert [line["verdict"] for line in check["lines"]] == ["met", "unknown"] and check["verdict"] == "partly"


def test_with_no_lines_of_done_the_outcome_itself_is_the_line(monkeypatch):
    judge = Judge(monkeypatch, ["met"])
    brain = install(Brain())
    job = job_store.create_job(title="T", goal="Find the cheapest train to Leeds.")
    brain.plan("job:", [[("say", "The 07:15, at £23.")]])
    run(job["id"])
    assert "1. Find the cheapest train to Leeds." in judge.prompts[0]
    assert objective.ledger_of(job_store.get_job(job["id"]))["check"]["verdict"] == "verified"


# --- verdicts, and what the person is told --------------------------------------------------------

def test_everything_done_is_delivered_as_verified(monkeypatch):
    Judge(monkeypatch, ["met", "met"])
    brain = install(Brain())
    job = new_job("the March report is filed", "Sam has been told")
    brain.plan("job:", [[FILE, TELL, ("say", "Filed the report and told Sam.")]])
    assert run(job["id"])["check"] == "verified"

    stored = job_store.get_job(job["id"])
    assert stored["status"] == "done" and stored["result"] == "Filed the report and told Sam."
    assert not stored["attempts"]
    [notice] = outbox_for(job["id"])
    assert notice["detail"]["check"] == "verified"
    assert notice["detail"]["checked"] == "Checked against what you asked: all of it is done."
    assert [r["summary"] for r in job_store.get_trace(job["id"]) if r["kind"] == "check"] == ["checked: verified"]
    # ...and Jarvis is told to say so with the result.
    from jarvis import prompt

    section = prompt.notices_section(outbox_for(job["id"]))
    assert "say this with the result" in section and "all of it is done" in section


def test_a_line_not_met_goes_back_alone_and_what_was_done_is_left_alone(monkeypatch):
    Judge(monkeypatch, ["met", "not_met"], ["met", "met"])
    brain = install(Brain())
    job = new_job("the March report is filed", "Sam has been told")
    brain.plan("job:", [[FILE, ("say", "Filed the report.")],
                        [TELL, ("say", "Filed the report and told Sam.")]])

    assert run(job["id"])["status"] == "stalled"
    [action] = carry_on()
    assert (action["action"], action["kind"]) == ("retried", "check")

    back = told(brain, job["id"], 1)
    assert 'Not yet done: "Sam has been told"' in back
    assert 'Already done and not to be touched again: "the March report is filed"' in back
    stored = job_store.get_job(job["id"])
    assert stored["status"] == "done" and stored["attempts"] == {"check": 1}
    assert objective.ledger_of(stored)["check"]["verdict"] == "verified"
    assert effect_count("report") == 1 and effect_count("told-sam") == 1


def test_work_that_stays_partly_done_is_delivered_as_partly_done_never_held_as_stuck(monkeypatch):
    """The result goes back as often as a result may be sent back, and then it is handed over
    for what it is. A check is a judgement too: work is not held on one."""
    Judge(monkeypatch, ["met", "not_met"], ["met", "not_met"], ["met", "not_met"])
    brain = install(Brain())
    job = new_job("the March report is filed", "Sam has been told")
    brain.plan("job:", [[FILE, ("say", "Filed the report. I could not reach Sam.")],
                        [("say", "Filed the report. Sam is still unreachable.")],
                        [("say", "Filed the report. Sam cannot be reached.")]])
    run(job["id"])
    carry_on()
    carry_on()

    stored = job_store.get_job(job["id"])
    assert stored["status"] == "done" and stored["attempts"] == {"check": 2}
    assert stored["result"] == "Filed the report. Sam cannot be reached."
    check = objective.ledger_of(stored)["check"]
    assert check["verdict"] == "partly"
    notices = outbox_for(job["id"])
    assert [n["reason"] for n in notices] == ["finished"]                # no "stuck" question
    said = notices[0]["detail"]["checked"]
    assert said == ("Checked against what you asked: 1 of 2 done. Not done: Sam has been told.")
    assert effect_count("report") == 1                                    # never filed again


def test_nothing_done_is_delivered_as_not_done(monkeypatch):
    Judge(monkeypatch, *[["not_met"]] * 3)
    brain = install(Brain())
    job = new_job("the March report is filed")
    brain.plan("job:", [[("say", "All done.")], [("say", "All done.")], [("say", "It is done.")]])
    run(job["id"])
    carry_on()
    carry_on()
    check = objective.ledger_of(job_store.get_job(job["id"]))["check"]
    assert check["verdict"] == "unmet"
    assert checking.label(check).startswith("Checked against what you asked: 0 of 1 done. Not done:")


def test_what_cannot_be_confirmed_is_said_and_not_sent_back(monkeypatch):
    Judge(monkeypatch, ["met", "unknown"])
    brain = install(Brain())
    job = new_job("the March report is filed", "Sam has read it")
    brain.plan("job:", [[FILE, ("say", "Filed. I cannot tell whether Sam has read it.")]])
    assert run(job["id"])["status"] == "done"
    stored = job_store.get_job(job["id"])
    assert not stored["attempts"]
    assert checking.label(objective.ledger_of(stored)["check"]) == (
        "Checked against what you asked: 1 of 2 done. Could not be confirmed: Sam has read it.")


def test_said_aloud_a_result_is_never_plainly_done_unless_it_checked_out(monkeypatch):
    spoken: list[str] = []
    monkeypatch.setattr(worker, "_may_speak", lambda: True)
    monkeypatch.setattr(worker, "_say", lambda text, *a, **k: spoken.append(text))
    Judge(monkeypatch, ["met", "unknown"], ["met"])
    brain = install(Brain())
    first = new_job("the March report is filed", "Sam has read it")
    brain.plan("job:", [[FILE, ("say", "Filed.")]])
    run(first["id"])
    second = new_job("the March report is filed")
    run(second["id"])
    assert "1 of 2 done. Could not be confirmed: Sam has read it." in spoken[0]
    assert spoken[1] == '"March" is done. Filed.'            # checked out: nothing to add


# --- recovery ---------------------------------------------------------------------------------------

def test_a_finish_cut_off_by_a_crash_checks_and_delivers_once(monkeypatch):
    judge = Judge(monkeypatch, ["met"])
    brain = install(Brain())
    job = new_job("the March report is filed")
    brain.plan("job:", [[FILE, ("say", "Filed.")]])
    real, crashed = worker._deliver_result, []

    def deliver(*args, **kw):
        real(*args, **kw)
        if not crashed:
            crashed.append(True)
            raise SimulatedCrash("the process died just after delivering")

    monkeypatch.setattr(worker, "_deliver_result", deliver)
    run(job["id"])
    restart(brain)
    orchestrator.recover_orphans(event_bus=EventBus())
    assert worker.join_all(timeout=20)

    stored = job_store.get_job(job["id"])
    assert stored["status"] == "done" and effect_count("report") == 1
    assert len(outbox_for(job["id"])) == 1
    assert len([r for r in job_store.get_trace(job["id"]) if r["kind"] == "check"]) == 1
    assert len(judge.prompts) <= 2


def test_a_crash_while_putting_a_sent_back_result_right_repeats_nothing(monkeypatch):
    Judge(monkeypatch, ["met", "not_met"], ["met", "met"])
    brain = install(Brain())
    job = new_job("the March report is filed", "Sam has been told")
    session = f"job:{job['id']}"
    brain.plan("job:", [[FILE, ("say", "Filed the report.")],
                        [TELL, ("call", "look_up_fact", {"q": "x"}), ("say", "Filed and told Sam.")]])
    run(job["id"])
    brain.crash_at.add((session, 1, 1))            # after Sam was told, before the round ended
    orchestrator.supervise(event_bus=EventBus())
    worker.join_all(timeout=20)

    restart(brain)
    orchestrator.recover_orphans(event_bus=EventBus())
    assert worker.join_all(timeout=20)
    assert job_store.get_job(job["id"])["status"] == "done"
    assert effect_count("report") == 1 and effect_count("told-sam") == 1


# --- an outward action is not done twice by accident ---------------------------------------------

def test_put_right_by_doing_it_all_over_again_runs_nothing_twice(monkeypatch):
    """Found live: a result sent back was "put right" by filing every report a second time."""
    Judge(monkeypatch, ["met", "not_met"], ["met", "met"])
    brain = install(Brain())
    job = new_job("the March report is filed", "Sam has been told")
    brain.plan("job:", [[FILE, ("say", "Filed the report.")],
                        [FILE, TELL, ("say", "Filed the report and told Sam.")]])     # files it AGAIN
    run(job["id"])
    carry_on()

    assert effect_count("report") == 1 and effect_count("told-sam") == 1
    refused = [r["result"] for r in results_seen(brain, job["id"]) if "error" in r["result"]]
    assert refused == [{"error": objective.REDO_REFUSAL}]
    stored = job_store.get_job(job["id"])
    assert stored["status"] == "done" and stored["attempts"] == {"check": 1}
    # Recorded as not run, and not counted against the work as a failure.
    filings = [a["result"] for a in checking.evidence(job["id"])["actions"] if a["args"] == {"tag": "report"}]
    assert filings == ["ok", "not_run"]
    assert checking.findings(checking.evidence(job["id"])) == []


class Doubling(Brain):
    """A model whose provider returns every tool call twice in one reply (measured live: about
    every other step on the person's own gateway)."""

    def stream(self, **kw):
        for event in super().stream(**kw):
            if isinstance(event, StepComplete) and event.tool_calls:
                [call] = event.tool_calls
                twin = ToolCall(id=call.id + "-again", name=call.name, args=dict(call.args))
                event = StepComplete(tool_calls=(call, twin), finish_reason="tool_calls", model_id="brain")
            yield event


def test_the_same_call_returned_twice_in_one_reply_runs_once(monkeypatch):
    Judge(monkeypatch, ["met", "met"])
    brain = install(Doubling())
    job = new_job("the March report is filed", "Sam has been told")
    brain.plan("job:", [[FILE, ("call", "look_up_fact", {"q": "sam"}), TELL, ("say", "Filed and told Sam.")]])
    run(job["id"])

    assert effect_count("report") == 1 and effect_count("told-sam") == 1
    stored = job_store.get_job(job["id"])
    assert stored["status"] == "done" and objective.ledger_of(stored)["check"]["verdict"] == "verified"
    # A read is nobody's business: both copies ran, neither was refused.
    reads = [a for a in checking.evidence(job["id"])["actions"] if a["name"] == "look_up_fact"]
    assert [a["result"] for a in reads] == ["ok", "ok"]


def test_a_repeat_that_is_asked_for_still_runs(monkeypatch):
    """"Send the reminder twice": told it is already done, the worker asks again — and it runs."""
    Judge(monkeypatch, ["met"])
    brain = install(Brain())
    job = new_job("the reminder has gone out twice", goal="Send Sam the reminder twice.")
    remind = ("call", "effect_external", {"tag": "reminder"})
    brain.plan("job:", [[remind, remind, remind, ("say", "Sent the reminder twice.")]])
    run(job["id"])

    assert effect_count("reminder") == 2
    how = [a["result"] for a in checking.evidence(job["id"])["actions"]]
    assert how == ["ok", "not_run", "ok"]
    stored = job_store.get_job(job["id"])
    check = objective.ledger_of(stored)["check"]
    # Reported as done twice, and it takes nothing away: that was the request.
    assert check["verdict"] == "verified" and [f["kind"] for f in check["facts"]] == ["twice"]
    assert checking.label(check) == "Checked against what you asked: all of it is done."


def test_a_third_time_has_to_be_asked_for_again(monkeypatch):
    Judge(monkeypatch, ["met"])
    brain = install(Brain())
    job = new_job("done")
    remind = ("call", "effect_external", {"tag": "reminder"})
    brain.plan("job:", [[remind, remind, remind, remind, ("say", "Done.")]])
    run(job["id"])
    assert [a["result"] for a in checking.evidence(job["id"])["actions"]] == ["ok", "not_run", "ok", "not_run"]
    assert effect_count("reminder") == 2


def test_the_same_action_with_different_details_is_a_different_action(monkeypatch):
    Judge(monkeypatch, ["met"])
    brain = install(Brain())
    job = new_job("done")
    brain.plan("job:", [[("call", "effect_external", {"tag": "to-sam"}),
                         ("call", "effect_external", {"tag": "to-ada"}),
                         ("call", "effect_low", {"tag": "note"}), ("call", "effect_low", {"tag": "note"}),
                         ("say", "Done.")]])
    run(job["id"])
    assert (effect_count("to-sam"), effect_count("to-ada"), effect_count("note")) == (1, 1, 2)
    assert all(a["result"] == "ok" for a in checking.evidence(job["id"])["actions"])


def test_a_round_replayed_after_a_crash_gets_the_answers_it_got_the_first_time(monkeypatch):
    """The call that ran comes back as its recorded result; the call that was refused is
    refused again — a crash must not turn "already done" into "do it again"."""
    Judge(monkeypatch, ["met"])
    brain = install(Brain())
    job = new_job("done")
    session = f"job:{job['id']}"
    remind = ("call", "effect_external", {"tag": "reminder"})
    brain.plan("job:", [[remind, remind, ("call", "look_up_fact", {"q": "x"}), ("say", "Sent.")]])
    brain.crash_at.add((session, 0, 2))            # after the second (refused) call came back
    run(job["id"])
    assert effect_count("reminder") == 1

    restart(brain)
    orchestrator.recover_orphans(event_bus=EventBus())
    assert worker.join_all(timeout=20)
    assert job_store.get_job(job["id"])["status"] == "done"
    assert effect_count("reminder") == 1
    results = [r["result"] for r in results_seen(brain, job["id"]) if r["name"] == "effect_external"]
    assert {"done": "reminder"} in results and {"error": objective.REDO_REFUSAL} in results


def test_started_over_from_the_beginning_it_really_is_all_done_again(monkeypatch):
    """A restart is the person asking for all of it again."""
    Judge(monkeypatch, ["met"], ["met"])
    brain = install(Brain())
    job = new_job("the March report is filed")
    brain.plan("job:", [[FILE, ("say", "Filed.")]])
    run(job["id"])
    run(job["id"], fresh=True)
    assert effect_count("report") == 2


def test_a_conversation_is_not_gated_at_all():
    """Only background work, where nobody is watching. In a chat the person is there."""
    from jarvis import chat_store
    from jarvis.orchestrator.pipeline import TurnRequest
    from jarvis.policy import Autonomy, Surface

    brain = install(Brain())
    session = chat_store.create_conversation()["id"]
    conversation.bind_session(session)
    brain.plan("", [[("call", "effect_low", {"tag": "twice"}), ("call", "effect_low", {"tag": "twice"}),
                     ("say", "Done twice.")]])
    list(assembly.get_orchestrator().run_turn(TurnRequest(
        text="do it twice", session_id=session, surface=Surface.TEXT, autonomy=Autonomy.INTERACTIVE)))
    assert effect_count("twice") == 2
