"""Delegation: Jarvis to specialists, and specialists to each other.

Driven through REAL turns — Jarvis's own `run_turn` on a chat session, calling the
real `ask_specialist` through the real executor and policy — with a scripted model
per speaker. The record that matters is `agent_runs`: who was asked, by whom, in
which chain, and what came back — read from the table, never from a model's claim.
"""

from __future__ import annotations

import threading

import pytest

from jarvis import assembly, conversation
from jarvis.agents import ensure_builtins, runner, store
from jarvis.capabilities.execute import execute_approved
from jarvis.db import reset_for_tests as reset_db
from jarvis.jobs import job_store, worker
from jarvis.orchestrator import ApprovalRequired, Done, ToolRan, TurnRequest
from jarvis.policy import Autonomy, CallContext, Surface

from session_scripted_model import SessionScriptedModel, install


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    conversation.reset_for_tests()
    assembly.reset_for_tests()
    ensure_builtins()
    yield
    worker.join_all(timeout=10)
    assembly.reset_for_tests()
    conversation.reset_for_tests()
    reset_db()


@pytest.fixture
def model():
    return install(assembly, SessionScriptedModel())


def jarvis_turn(text: str, session: str = "conv1") -> list:
    return list(assembly.get_orchestrator().run_turn(
        TurnRequest(text=text, session_id=session, surface=Surface.TEXT,
                    autonomy=Autonomy.INTERACTIVE)))


def done_text(events: list) -> str:
    return next(e.text for e in events if isinstance(e, Done))


def tool_results(model: SessionScriptedModel, speaker: str) -> list:
    """Every tool result that came back to this speaker, in order."""
    out = []
    for request in model.requests_of(speaker):
        last = request["messages"][-1] if request["messages"] else {}
        out += last.get("toolResults") or []
    return out


# --- Jarvis decides ----------------------------------------------------------

def test_jarvis_is_offered_the_specialists_and_can_simply_answer(model):
    model.on("jarvis").says("It's 4 o'clock.")
    events = jarvis_turn("what's 2 + 2")
    assert done_text(events) == "It's 4 o'clock."
    [request] = model.requests_of("jarvis")
    assert "ask_specialist" in request["tools"]
    assert "Your specialist agents" in request["system"]
    assert store.list_runs() == []  # nobody was asked


def test_one_specialist_does_the_work_and_jarvis_combines_it(model):
    model.on("jarvis").calls_tool("ask_specialist", {
        "agent": "research", "task": "Find the 2025 size of the UK home espresso market.",
        "context": "The operator is thinking of opening an espresso cart."})
    model.on("research").says("The UK home espresso market was about GBP 1.1bn in 2025 [source].")
    model.on("jarvis").says("Research found it was about 1.1 billion pounds in 2025.")

    events = jarvis_turn("How big is the UK home espresso market?")
    assert done_text(events).startswith("Research found")
    [run] = store.list_runs()
    assert run["agentId"] == "research" and run["requestedBy"] == "jarvis"
    assert run["conversationId"] == "conv1" and run["status"] == "done"
    # The context Jarvis gave reached the specialist.
    assert "espresso cart" in model.requests_of("research")[0]["messages"][-1]["text"]
    # And the specialist's work came back to Jarvis as the tool result.
    [result] = tool_results(model, "jarvis")
    assert result["result"]["result"].startswith("The UK home espresso market")
    assert any(isinstance(e, ToolRan) and e.capability == "ask_specialist" and e.ok
               for e in events)


def test_several_specialists_each_do_their_part(model):
    model.on("jarvis").calls_tool("ask_specialist", {"agent": "research", "task": "Market size"})
    model.on("research").says("Market: 1.1bn.")
    model.on("jarvis").calls_tool("ask_specialist", {"agent": "content",
                                                     "task": "Launch post using: Market 1.1bn"})
    model.on("content").says("Post: Coffee lovers, 1.1bn reasons...")
    model.on("jarvis").says("Here's the research and a launch post.")

    jarvis_turn("Research the market and write me a launch post")
    runs = store.list_runs()
    assert sorted(r["agentId"] for r in runs) == ["content", "research"]
    assert all(r["depth"] == 1 and r["requestedBy"] == "jarvis" for r in runs)


# --- specialists delegate to each other --------------------------------------

def test_a_specialist_asks_its_collaborators_and_the_tree_is_recorded(model):
    model.on("jarvis").calls_tool("ask_specialist", {
        "agent": "advertising", "task": "Plan a GBP 500 Instagram campaign for the cart."})
    model.on("advertising").calls_tool("ask_specialist", {
        "agent": "research", "task": "Who buys specialty coffee from carts in Leeds?"})
    model.on("research").says("Commuters 25-40, near stations.")
    model.on("advertising").calls_tool("ask_specialist", {
        "agent": "content", "task": "Three ad captions for commuters 25-40."})
    model.on("content").says("1. Beat the queue. 2. ... 3. ...")
    model.on("advertising").says("Campaign plan: target commuters 25-40, captions attached.")
    model.on("jarvis").says("Here's the campaign plan.")

    jarvis_turn("Plan my Instagram ads")
    runs = {r["agentId"]: r for r in store.list_runs()}
    ads = runs["advertising"]
    assert ads["depth"] == 1 and ads["requestedBy"] == "jarvis"
    for helper in ("research", "content"):
        assert runs[helper]["parentRunId"] == ads["id"]
        assert runs[helper]["rootRunId"] == ads["id"]
        assert runs[helper]["depth"] == 2 and runs[helper]["requestedBy"] == "advertising"
        assert runs[helper]["conversationId"] == "conv1"
    # Advertising got its helpers' work back, and Jarvis is told who helped.
    assert [r["result"]["result"] for r in tool_results(model, "advertising")] == [
        "Commuters 25-40, near stations.", "1. Beat the queue. 2. ... 3. ..."]
    [to_jarvis] = tool_results(model, "jarvis")
    assert {h["agent"] for h in to_jarvis["result"]["askedForHelp"]} == {
        "Research & Intelligence", "Content"}
    # A specialist's prompt names the colleagues it may ask.
    assert "- research — Research & Intelligence" in model.requests_of("advertising")[0]["system"]


def test_a_specialist_may_only_ask_its_own_collaborators(model):
    model.on("jarvis").calls_tool("ask_specialist", {"agent": "research", "task": "t"})
    model.on("research").calls_tool("ask_specialist", {"agent": "sales", "task": "t"})
    model.on("research").says("Did it myself.")
    model.on("jarvis").says("ok")
    jarvis_turn("go")
    [refused] = tool_results(model, "research")
    assert refused["result"]["ok"] is False
    assert "isn't set up to ask Sales & CRM" in refused["result"]["error"]
    assert [r["agentId"] for r in store.list_runs()] == ["research"]


def test_a_chain_cannot_come_back_round_or_go_too_deep(model):
    for name in ("Alpha", "Beta", "Gamma", "Delta"):
        store.create_agent({"name": name, "collaborators": "any",
                            "capabilityAccess": {"mode": "selected", "names": []}})
    assembly.get_registry()  # sync the roster
    from jarvis.agents.capabilities import sync
    sync(assembly.get_registry())

    model.on("jarvis").calls_tool("ask_specialist", {"agent": "alpha", "task": "t"})
    model.on("alpha").calls_tool("ask_specialist", {"agent": "beta", "task": "t"})
    model.on("beta").calls_tool("ask_specialist", {"agent": "alpha", "task": "back to you"})
    model.on("beta").calls_tool("ask_specialist", {"agent": "gamma", "task": "t"})
    model.on("gamma").calls_tool("ask_specialist", {"agent": "delta", "task": "t"})
    model.on("gamma").says("gamma did it")
    model.on("beta").says("beta done")
    model.on("alpha").says("alpha done")
    model.on("jarvis").says("done")
    jarvis_turn("go")

    beta_results = [r["result"] for r in tool_results(model, "beta")]
    assert beta_results[0]["ok"] is False and "go round in a circle" in beta_results[0]["error"]
    gamma_results = [r["result"] for r in tool_results(model, "gamma")]
    assert gamma_results[0]["ok"] is False and "as far as it can go" in gamma_results[0]["error"]
    depths = {r["agentId"]: r["depth"] for r in store.list_runs()}
    assert depths == {"alpha": 1, "beta": 2, "gamma": 3}


def test_one_request_cannot_fan_out_past_its_budget(model, monkeypatch):
    monkeypatch.setattr(runner, "MAX_RUNS_PER_ROOT", 2)
    model.on("jarvis").calls_tool("ask_specialist", {"agent": "advertising", "task": "t"})
    model.on("advertising").calls_tool("ask_specialist", {"agent": "research", "task": "a"})
    model.on("research").says("r")
    model.on("advertising").calls_tool("ask_specialist", {"agent": "content", "task": "b"})
    model.on("advertising").says("done")
    model.on("jarvis").says("done")
    jarvis_turn("go")
    refused = tool_results(model, "advertising")[1]["result"]
    assert refused["ok"] is False and "as many specialists as it can" in refused["error"]


def test_a_disabled_or_unknown_specialist_is_refused_in_words(model):
    store.update_agent("scout", {"enabled": False})
    model.on("jarvis").calls_tool("ask_specialist", {"agent": "scout", "task": "hunt"})
    model.on("jarvis").says("Scout is off.")
    jarvis_turn("hunt for me")
    [result] = tool_results(model, "jarvis")
    assert result["result"]["ok"] is False and "turned off" in result["result"]["error"]


# --- approvals and notes -----------------------------------------------------

def test_a_specialists_approval_reaches_the_person_and_only_they_can_answer(model):
    model.on("jarvis").calls_tool("ask_specialist", {"agent": "content",
                                                     "task": "Save the post as post.docx"})
    model.on("content").calls_tool("create_artifact", {"filename": "post.docx",
                                                       "paragraphs": ["Coffee!"]})
    events = jarvis_turn("save it as a Word file")

    [asked] = [e for e in events if isinstance(e, ApprovalRequired)]
    assert asked.capability == "create_artifact"
    assert "Content needs your go-ahead" in asked.reason
    # Jarvis's turn stopped at the question — no reply was generated over it.
    assert not any(isinstance(e, Done) for e in events)
    [run] = store.list_runs()
    assert run["status"] == "awaiting_approval" and run["approvalId"] == asked.approval_id

    # The person answers in a later turn, and only then does it run.
    ctx = CallContext(session_id=run["sessionId"], turn_id="later-turn", surface=Surface.TEXT,
                      autonomy=Autonomy.INTERACTIVE)
    result = execute_approved(asked.approval_id, "later-turn", ctx,
                              registry=assembly.get_registry())
    assert result.ok, result.error


def test_an_agent_keeps_its_own_notes_and_nobody_elses(model):
    model.on("teacher").calls_tool("write_my_note", {"topic": "learner: python",
                                                     "text": "Knows loops; next: functions"})
    model.on("teacher").says("Saved.")
    runner.run_agent("teacher", "Record progress", conversation_id="c1")
    model.on("teacher").calls_tool("read_my_notes", {"topic": "learner: python"})
    model.on("teacher").says("Next up is functions.")
    runner.run_agent("teacher", "What's next?", conversation_id="c2")
    read = tool_results(model, "teacher")[-1]["result"]
    assert read["notes"][0]["text"] == "Knows loops; next: functions"
    assert store.list_notes("scout") == []

    # Jarvis has no notes: no agent is behind its turn.
    model.on("jarvis").calls_tool("read_my_notes", {})
    model.on("jarvis").says("no")
    jarvis_turn("read notes")
    assert "Only a specialist" in tool_results(model, "jarvis")[0]["result"]["error"]


# --- background --------------------------------------------------------------

def test_long_work_goes_to_the_background_as_that_specialist(model):
    model.on("jarvis").calls_tool("ask_specialist", {
        "agent": "scout", "task": "Hunt for grants for small food businesses in Leeds.",
        "background": True})
    model.on("jarvis").says("Scout's on it in the background.")
    model.on("scout").says("Found 2 grants worth your time: ...")
    events = jarvis_turn("hunt for grants for me, take your time")
    assert done_text(events) == "Scout's on it in the background."
    assert worker.join_all(timeout=10)

    [job] = job_store.list_jobs()
    assert job["agentId"] == "scout" and job["status"] == "done"
    assert job["result"].startswith("Found 2 grants")
    [run] = store.list_runs()
    assert run["agentId"] == "scout" and run["jobId"] == job["id"]
    assert run["requestedBy"] == "job" and run["sessionId"] == f"job:{job['id']}"
    assert model.requests_of("scout")[0]["system"].startswith("You are Scout")


# --- no deadlock ---------------------------------------------------------------

def test_many_delegation_chains_at_once_all_finish(model):
    """Every chain here holds a worker while it waits for a nested turn; with the
    waits and the work on the same pool this is where it would lock up."""
    from jarvis.orchestrator.model_port import ToolCall

    def after_tool(then_call: ToolCall | None, answer: str):
        def respond(messages):
            if messages and messages[-1].get("toolResults"):
                return ("say", answer)
            return ("call", then_call) if then_call else ("say", answer)
        return respond

    chains = 10
    model.on("jarvis").responds(after_tool(
        ToolCall(id="j1", name="ask_specialist", args={"agent": "advertising", "task": "t"}), "j"))
    model.on("advertising").responds(after_tool(
        ToolCall(id="a1", name="ask_specialist", args={"agent": "research", "task": "t"}), "a"))
    model.on("research").responds(after_tool(
        ToolCall(id="r1", name="read_my_notes", args={}), "r"))
    threads = [threading.Thread(target=jarvis_turn, args=("go", f"conv{i}")) for i in range(chains)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads), "a delegation chain locked up"
    runs = store.list_runs(limit=100)
    assert len(runs) == chains * 2 and all(r["status"] == "done" for r in runs)
