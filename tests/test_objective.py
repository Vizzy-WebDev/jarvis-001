"""Taking work on: the brief, the ledger, the worker's own prompt, and the two ways in.

Real turn loop, executor, job worker and durable runner; the model is `durable_support.Brain`.
What is asserted is what was stored and what the model was actually sent.
"""

from __future__ import annotations

import pytest

from durable_support import Brain, effect_count, install, register_effects, run_and_wait
from jarvis import assembly, background, chat_store, conversation, durable
from jarvis.capabilities import search
from jarvis.db import reset_for_tests as reset_db
from jarvis.events.bus import EventBus
from jarvis.jobs import job_store, objective, worker
from jarvis.orchestrator.pipeline import MAX_STEPS, Done, Failed, TurnRequest
from jarvis.policy import Autonomy, Surface


@pytest.fixture(autouse=True)
def _isolate(scratch, monkeypatch):
    reset_db()
    conversation.reset_for_tests()
    assembly.reset_for_tests()
    durable.reset_for_tests()
    monkeypatch.setattr("jarvis.jobs.worker._verify_result", lambda job, answer: None)
    register_effects()
    yield
    assert worker.join_all(timeout=20)
    assert background.join_all(timeout=20)
    durable.reset_for_tests()
    assembly.reset_for_tests()
    conversation.reset_for_tests()
    reset_db()


def chat() -> str:
    session = chat_store.create_conversation()["id"]
    conversation.bind_session(session)
    return session


def say(session: str, text: str) -> list:
    return list(assembly.get_orchestrator().run_turn(TurnRequest(
        text=text, session_id=session, surface=Surface.TEXT, autonomy=Autonomy.INTERACTIVE)))


def system_of(call: dict) -> str:
    return str(call["system"])


# --- the brief and the ledger -------------------------------------------------------------------

def test_a_job_made_with_only_a_goal_still_has_a_brief():
    job = job_store.create_job(title="T", goal="find the facts")
    brief = objective.brief_of(job_store.get_job(job["id"]))
    assert brief["outcome"] == "find the facts" and brief["doneWhen"] == []
    assert objective.opening_text(job) == "find the facts"


def test_the_brief_is_kept_and_opens_the_work_with_what_done_looks_like():
    brief = objective.make_brief(outcome="File the March total", done_when=["a report is filed", "it names the total"],
                                 context="USD only", said=["add up March"], already="- read x → 5")
    job = job_store.create_job(title="T", goal="File the March total", brief=brief)
    stored = job_store.get_job(job["id"])
    assert stored["brief"] == brief
    assert objective.opening_text(stored) == \
        "File the March total\n\nIt is done when:\n- a report is filed\n- it names the total"


def test_the_ledger_holds_the_plan_what_was_found_and_progress():
    job = job_store.create_job(title="T", goal="g")
    ledger = objective.update_ledger(job["id"], steps=[{"text": "read them", "status": "done"},
                                                       {"text": "file it", "status": "weird"}, "check it"],
                                     found=["total 5"], open_questions=["which month?"])
    assert [(s["text"], s["status"]) for s in ledger["steps"]] == \
        [("read them", "done"), ("file it", "todo"), ("check it", "todo")]
    assert job_store.get_job(job["id"])["progress"] == 33
    again = objective.update_ledger(job["id"], found=["total 5", "largest is A"])
    assert again["found"] == ["total 5", "largest is A"] and len(again["steps"]) == 3   # plan untouched
    objective.note_round(job["id"], "<think>hmm\n\nHalfway: two of four read.")
    assert objective.ledger_of(job_store.get_job(job["id"]))["note"] == "Halfway: two of four read."


def test_old_findings_make_room_and_the_worker_is_told():
    job = job_store.create_job(title="T", goal="g")
    objective.update_ledger(job["id"], found=[f"fact {n}" for n in range(objective.MAX_FOUND)])
    ledger = objective.update_ledger(job["id"], found=["one more"])
    assert ledger["dropped"] == 1 and ledger["found"][-1] == "one more" and len(ledger["found"]) == objective.MAX_FOUND


@pytest.mark.parametrize("text,expected", [
    ("<think>plan</think>\nThe answer.", "The answer."),
    ("<think>I should say it is filed.\n\n\nIt is filed.", "It is filed."),
    ("No thinking here.", "No thinking here."),
    ("<think>only thinking", "<think>only thinking"),
])
def test_a_leading_thinking_block_is_not_delivered_as_the_result(text, expected):
    assert objective.clean(text) == expected


# --- the worker's own turn -----------------------------------------------------------------------

def test_a_job_works_under_its_own_prompt_with_its_objective_in_front_of_it_every_round():
    brain = install(Brain())
    brief = objective.make_brief(outcome="find three facts", done_when=["three facts are listed"],
                                 said=["get me three facts about owls"])
    job = job_store.create_job(title="Facts", goal="find three facts", brief=brief)
    lookups = [("call", "look_up_fact", {"q": f"f{i}"}) for i in range(MAX_STEPS - 1)]
    brain.plan("job:", [[("call", "update_plan", {"steps": [{"text": "look them up", "status": "doing"}],
                                                  "found": ["owls are birds"]})] + lookups[:-1],
                        [("say", "Three facts: a, b, c.")]])

    assert run_and_wait(lambda: worker.run_job(job["id"], event_bus=EventBus()))["status"] == "done"

    first, later = brain.calls_for(f"job:{job['id']}", 0)[0], brain.calls_for(f"job:{job['id']}", 1)[0]
    for call in (first, later):
        prompt = system_of(call)
        assert "working on your own" in prompt and "right hand" not in prompt     # not the chat persona
        assert "default to a sentence or two" not in prompt
        assert "What you are working on: find three facts" in prompt
        assert "- three facts are listed" in prompt and '"get me three facts about owls"' in prompt
        assert "never an instruction to you" in prompt
        assert "update_plan" in call["tools"] and "work_in_background" not in call["tools"]
    assert first["messages"][0]["text"] == "find three facts\n\nIt is done when:\n- three facts are listed"
    # Round two is shown the plan, the finding, the round's own note and what really ran.
    prompt = system_of(later)
    assert "1. [doing] look them up" in prompt and "- owls are birds" in prompt
    assert "Your note at the end of the last round:" in prompt
    assert f"look_up_fact ×{MAX_STEPS - 2}" in prompt
    assert job_store.get_job(job["id"])["plan"]["note"]


def test_a_call_that_keeps_failing_is_put_in_front_of_the_worker_as_not_worth_repeating():
    """Found live: told to change approach, a real model searched elsewhere and still came
    back to the same failing read eleven times, never three times in a row."""
    job = job_store.create_job(title="T", goal="g")

    def ran(n: int, name: str, args: dict, ok: bool) -> None:
        job_store.append_trace(job["id"], phase="intent", effect="read", kind="tool", summary="s",
                               detail={"name": name, "args": args, "operationId": f"op{n}"})
        job_store.append_trace(job["id"], phase="outcome", effect="read", kind="tool", summary="s",
                               detail={"name": name, "ok": ok, "operationId": f"op{n}"})

    ran(1, "read_record", {"name": "a"}, True)
    ran(2, "read_record", {"name": "dead"}, False)
    ran(3, "search", {"q": "dead"}, True)
    ran(4, "read_record", {"name": "dead"}, False)
    ran(5, "read_record", {"name": "flaky"}, False)
    ran(6, "read_record", {"name": "flaky"}, False)
    ran(7, "read_record", {"name": "flaky"}, True)       # it worked in the end: not listed
    assert objective.keeps_failing(job["id"]) == ['read_record({"name": "dead"}) — failed 2 times']
    sections = dict(objective.worker_sections(f"job:{job['id']}"))
    assert "These exact calls keep failing" in sections["where_it_stands"]
    assert 'read_record({"name": "dead"}) — failed 2 times' in sections["where_it_stands"]


def test_an_exact_call_that_has_failed_three_times_is_not_made_a_fourth():
    """Found live: shown that a read "keeps failing", a real model still made it 13 times."""
    from durable_support import _effect_spec  # noqa: F401 — the registry is already populated
    from jarvis.capabilities import CapabilitySpec, Risk

    def always_fails(name: str = "") -> dict:
        # The way tools usually fail: they run, and say it did not work.
        return {"ok": False, "error": f'The records system timed out reading "{name}".'}

    brain = install(Brain())
    assembly.get_registry().register(CapabilitySpec(
        id="test.read_dead", name="read_dead", description="Reads a record (test).",
        input_schema={"type": "object", "properties": {"name": {"type": "string"}}},
        handler=always_fails, risk=Risk.LOW, tags=frozenset({"core"})))
    job = job_store.create_job(title="T", goal="read it")
    dead = ("call", "read_dead", {"name": "x"})
    other = ("call", "look_up_fact", {"q": "something else"})
    # Never three in a row, so the repetition check alone would not see it.
    brain.plan("job:", [[dead, other, dead, other, dead, other, dead],
                        [dead, ("call", "read_dead", {"name": "y"}), ("say", "Could not read x.")]])

    run_and_wait(lambda: worker.run_job(job["id"], event_bus=EventBus()))

    ran = [r for r in get_ops(job["id"]) if r == "read_dead"]
    assert len(ran) == 3 + 1                # x three times and never again; y (different) once
    refused = [res["result"]["error"] for call in brain.calls_for(f"job:{job['id']}")
               for m in call["messages"][-1:] if m.get("role") == "tool"
               for res in m.get("toolResults") or [] if res.get("name") == "read_dead"
               and "Not run" in str(res.get("result"))]
    assert refused and "already failed 3 times" in refused[0] and "cannot_finish" in refused[0]
    # And the record says those calls failed, though each one "completed".
    # (Refused tries are on record as failures too, so the count is three or more.)
    assert any(line.startswith('read_dead({"name": "x"}) — failed ') for line in objective.keeps_failing(job["id"]))


def get_ops(job_id: str) -> list[str]:
    from jarvis.db import get_db

    return [r[0] for r in get_db().execute(
        "SELECT capability FROM operations WHERE operation_id LIKE ? ORDER BY completed_at",
        (f"{job_id}/%",)).fetchall()]


def test_a_request_the_model_garbled_is_not_run_and_the_model_can_ask_again():
    """Found live: one malformed tool call on the second step of a reply ended the whole
    turn, and the objective it was about to take on was never taken on."""
    from jarvis.orchestrator.model_port import StepComplete, TextChunk, ToolCall

    class Garbles(Brain):
        def stream(self, **kw):
            step = len([m for m in kw["messages"] if m.get("role") == "assistant"])
            self.calls.append({"session": kw["session_id"], "round": 0, "step": step,
                               "system": kw["system"], "tools": [], "messages": kw["messages"]})
            if step == 0:
                yield StepComplete(tool_calls=(ToolCall(id="c1", name="effect_low", args={}, garbled=True),),
                                   finish_reason="tool_calls", model_id="brain")
            elif step == 1:
                yield StepComplete(tool_calls=(ToolCall(id="c2", name="effect_low", args={"tag": "second-try"}),),
                                   finish_reason="tool_calls", model_id="brain")
            else:
                yield TextChunk("Done on the second try.")
                yield StepComplete(text="Done on the second try.", model_id="brain")

    brain = install(Garbles())
    session = chat()
    events = say(session, "Do the thing.")

    assert next(e for e in events if isinstance(e, Done)).text == "Done on the second try."
    assert not any(isinstance(e, Failed) for e in events)
    assert effect_count() == 1 and effect_count("second-try") == 1      # the garbled one never ran
    told = brain.calls[1]["messages"][-1]["toolResults"][0]["result"]["error"]
    assert "could not be read" in told and "NOT run" in told


def test_update_plan_is_a_workers_tool_and_nobody_elses():
    install(Brain())
    registry = assembly.get_registry()
    spec = registry.get("update_plan")
    assert spec.has_tag("job")
    from jarvis.policy import CallContext

    refused = spec.handler(steps=[], ctx=CallContext(session_id="chat-1", turn_id="t", surface=Surface.TEXT,
                                                     autonomy=Autonomy.INTERACTIVE))
    assert refused["ok"] is False


# --- the front door -------------------------------------------------------------------------------

def test_the_ways_a_promise_becomes_real_are_always_in_front_of_jarvis():
    """They were reachable only through find_capability, and across 41 real conversations
    work_in_background was called zero times."""
    brain = install(Brain())
    session = chat()
    brain.plan("", [[("say", "Hello.")]])
    say(session, "hello")
    declared = brain.calls[0]["tools"]
    assert {"work_in_background", "schedule_task", "watch_for"} <= set(declared)
    assert "update_plan" not in declared


def test_taking_work_on_from_a_chat_records_a_brief_with_their_own_words(monkeypatch):
    brain = install(Brain())
    session = chat()
    conversation.push_user_text(session, "Only count USD invoices, by the way.")
    conversation.push_assistant_text(session, "Noted.")
    brain.plan("job:", [[("say", "Done: total 5.")]])
    brain.plan("", [[], [("call", "look_up_fact", {"q": "where the invoices are"}),
                         ("call", "work_in_background",
                          {"goal": "Total the March invoices and file a report",
                           "done_when": ["a report with the total is filed"], "context": "USD only"}),
                         ("say", "On it, Boss.")]])

    events = say(session, "Total the March invoices and file a report.")

    assert any(isinstance(e, Done) for e in events)
    assert worker.join_all(timeout=20)
    [job] = job_store.list_jobs()
    assert job["conversationId"] == session          # scoped to the chat that asked
    brief = job["brief"]
    assert brief["outcome"] == "Total the March invoices and file a report"
    assert brief["doneWhen"] == ["a report with the total is filed"] and brief["context"] == "USD only"
    assert brief["said"] == ["Only count USD invoices, by the way.", "Total the March invoices and file a report."]
    assert "look_up_fact(" in brief["already"] and "facts about where the invoices are" in brief["already"]
    # And the worker was shown all of it.
    prompt = system_of(brain.calls_for(f"job:{job['id']}")[0])
    assert "USD only" in prompt and "Already done before this was handed to you" in prompt


def test_work_that_does_not_fit_in_a_reply_is_carried_on_not_abandoned():
    """Before: "I went round 8 times without finishing that, so I stopped. Tell me what to try
    differently." Now the last step offers exactly one thing — carrying it on."""
    brain = install(Brain())
    session = chat()
    lookups = [("call", "look_up_fact", {"q": f"f{i}"}) for i in range(MAX_STEPS - 1)]
    brain.plan("job:", [[("call", "effect_low", {"tag": "finished-in-background"}), ("say", "All found.")]])
    brain.plan("", [lookups + [("call", "work_in_background",
                                {"goal": "find all the facts", "done_when": ["every fact is found"]})]])

    events = say(session, "Find all the facts.")

    assert not any(isinstance(e, Failed) for e in events)
    done = next(e for e in events if isinstance(e, Done))
    assert done.text.startswith("Working on") and "in the background" in done.text
    last = brain.calls_for(session)[-1]
    assert last["tools"] == ["work_in_background"]                      # the only thing on offer
    assert "do not leave it half done" in system_of(last)
    assert worker.join_all(timeout=20)
    [job] = job_store.list_jobs()
    assert job["status"] == "done" and effect_count("finished-in-background") == 1
    assert job["brief"]["already"].count("look_up_fact(") == MAX_STEPS - 1   # what the reply had found
    # The chat holds a real reply, and the tool call that started the work.
    stored = chat_store.get_messages(session)
    assert stored[-1]["role"] == "assistant" and stored[-1]["text"] == done.text


def test_work_still_under_way_at_the_last_step_is_handed_over_even_if_the_model_does_not_ask():
    """Found live: offered only the hand-off on its last step, a real model asked for one more
    read instead, and the reply ended empty with eleven records read and nothing filed."""
    brain = install(Brain())
    session = chat()
    lookups = [("call", "look_up_fact", {"q": f"f{i}"}) for i in range(MAX_STEPS)]   # never stops reading
    brain.plan("job:", [[("say", "All found, and filed.")]])
    brain.plan("", [lookups])

    events = say(session, "Find all the facts and file them.")

    done = next(e for e in events if isinstance(e, Done))
    assert "in the background" in done.text and not any(isinstance(e, Failed) for e in events)
    assert worker.join_all(timeout=20)
    [job] = job_store.list_jobs()
    assert job["status"] == "done" and job["goal"] == "Find all the facts and file them."
    assert job["brief"]["said"] == ["Find all the facts and file them."]
    assert job["brief"]["already"].count("look_up_fact(") == MAX_STEPS - 1
    assert job["conversationId"] == session
    # The read it asked for on its last step was never run, and the transcript is still valid:
    # every stored call has its result.
    calls = [c["id"] for m in conversation.get_messages(session) for c in m.get("toolCalls") or []]
    results = [r["id"] for m in conversation.get_messages(session) for r in m.get("toolResults") or []]
    assert sorted(calls) == sorted(results) and len(calls) == MAX_STEPS


def _outage():
    from jarvis.orchestrator.model_port import ModelUnavailable

    return ModelUnavailable("stub.test had a problem on its end.",
                            detail={"reason": "unavailable", "scope": "endpoint", "recovery": "after"})


class _DropsOut(Brain):
    """Fails the chat's call at `at` (a step number) with whatever `failure()` makes."""

    def __init__(self, at: int, failure) -> None:
        super().__init__()
        self.at, self.failure, self.dropped = at, failure, False

    def stream(self, **kw):
        from durable_support import position

        if not kw["session_id"].startswith("job:") and position(kw["messages"])[1] == self.at \
                and not self.dropped:
            self.dropped = True
            raise self.failure()
        return super().stream(**kw)


def test_work_under_way_when_the_model_drops_out_is_carried_on_in_the_background():
    """Found live: eight steps into a reply the chosen model had a problem on its end, and
    the reply ended as an error with everything it had read thrown away."""
    brain = install(_DropsOut(at=3, failure=_outage))
    session = chat()
    lookups = [("call", "look_up_fact", {"q": f"f{i}"}) for i in range(5)]
    brain.plan("job:", [[("call", "effect_low", {"tag": "carried-on"}), ("say", "Finished what the reply began.")]])
    brain.plan("", [lookups])

    events = say(session, "Find the five facts and file them.")

    assert not any(isinstance(e, Failed) for e in events)
    done = next(e for e in events if isinstance(e, Done))
    assert "stopped answering part-way through" in done.text and "in the background" in done.text
    assert worker.join_all(timeout=20)
    [job] = job_store.list_jobs()
    assert job["status"] == "done" and effect_count("carried-on") == 1
    assert job["goal"] == "Find the five facts and file them." and job["conversationId"] == session
    assert job["brief"]["already"].count("look_up_fact(") == 3        # the three steps that had run
    calls = [c["id"] for m in conversation.get_messages(session) for c in m.get("toolCalls") or []]
    results = [r["id"] for m in conversation.get_messages(session) for r in m.get("toolResults") or []]
    assert sorted(calls) == sorted(results)                             # the transcript is still valid


def test_an_outage_before_any_work_is_just_reported():
    brain = install(_DropsOut(at=0, failure=_outage))
    session = chat()
    events = say(session, "Find the five facts.")
    assert [e.code for e in events if isinstance(e, Failed)] == ["no_model"]
    assert job_store.list_jobs() == []


def test_a_request_the_model_refuses_mid_work_is_not_carried_on():
    """Carrying it on would only send the same refused request again."""
    from jarvis.orchestrator.model_port import ModelUnavailable

    refused = lambda: ModelUnavailable("stub.test couldn't use that request.",   # noqa: E731
                                       detail={"reason": "invalid_request", "scope": "request"})
    brain = install(_DropsOut(at=2, failure=refused))
    session = chat()
    brain.plan("", [[("call", "look_up_fact", {"q": f"f{i}"}) for i in range(4)]])
    events = say(session, "Find the facts.")
    assert any(isinstance(e, Failed) for e in events) and job_store.list_jobs() == []


def test_a_reply_that_is_finished_on_its_last_step_just_answers():
    brain = install(Brain())
    session = chat()
    lookups = [("call", "look_up_fact", {"q": f"f{i}"}) for i in range(MAX_STEPS - 1)]
    brain.plan("", [lookups + [("say", "Here are the facts.")]])
    events = say(session, "Find the facts.")
    assert next(e for e in events if isinstance(e, Done)).text == "Here are the facts."
    assert job_store.list_jobs() == []


def test_background_work_is_never_offered_the_hand_off():
    brain = install(Brain())
    job = job_store.create_job(title="T", goal="g")
    brain.plan("job:", [[("say", "done")]])
    run_and_wait(lambda: worker.run_job(job["id"], event_bus=EventBus()))
    assert "work_in_background" not in brain.calls_for(f"job:{job['id']}")[0]["tools"]


# --- finding what a request needs, without being asked ------------------------------------------

@pytest.mark.parametrize("said,wanted,unwanted", [
    ("Add up the March invoices in the office records and file a report there",
     {"office_file_report", "office_list_records", "office_read_record"}, {"office_send_message"}),
    ("Stop watching for that file", {"stop_watching"}, {"stop_working_on"}),
    # Words many names share say nothing by themselves; a verb alone never picks a capability.
    ("Check the files", set(), {"check_claim", "check_on_work", "read_skill_file"}),
    ("Task from Jarvis:\nCheck the files", set(), {"cancel_task", "list_tasks"}),
    ("Start at the place named x", set(), {"start_project"}),
    ("Morning!", set(), set()),
])
def test_only_what_a_request_plainly_points_at_is_declared_unasked(said, wanted, unwanted):
    """Against the real registry (plus the proving world's tools): rarity is a fact about the
    whole catalogue, so a toy one would test nothing."""
    from proving import world

    install(Brain())
    world.install()
    everything = assembly.get_registry().list()
    candidates = [s for s in everything if not s.has_tag("core") and not s.has_tag("job")]
    found = set(search.relevant(candidates, said, among=everything))
    assert wanted <= found and not (unwanted & found), found
    if not wanted:
        assert found == set()


def test_the_turn_loop_declares_what_the_request_points_at_before_asking_the_model():
    """Found live: asked about "the office records", a real model never searched — it tried a
    file tool it could see and then asked the person where the records were."""
    from proving import world

    brain = install(Brain())
    world.install()
    session = chat()
    brain.plan("", [[("say", "ok")]])
    say(session, "Add up the March invoices in the office records and file a report there.")
    declared = set(brain.calls[0]["tools"])
    assert {"office_list_records", "office_read_record", "office_file_report"} <= declared
    assert "office_send_message" not in declared
