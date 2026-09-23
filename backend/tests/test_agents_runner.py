"""Running an agent: the real orchestrator, the real executor and policy, and a
scripted model at the port.

What is pinned here: an agent's turn gets ITS identity and doctrine (not
Jarvis's), only the capabilities its access allows are callable — whatever the
model asks for — its model pin is passed through untouched, its Memory setting is
honoured, every run is recorded, and an approval inside a specialist stops the run
rather than being answered by it.
"""

from __future__ import annotations

import pytest

from jarvis import assembly, conversation
from jarvis.agents import ensure_builtins, runner, store
from jarvis.db import reset_for_tests as reset_db
from jarvis.events import EventType
from jarvis.events.bus import bus
from jarvis.memory import store as memory_store
from jarvis.policy import Autonomy

from session_scripted_model import SessionScriptedModel, install


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    conversation.reset_for_tests()
    assembly.reset_for_tests()
    ensure_builtins()
    yield
    assembly.reset_for_tests()
    conversation.reset_for_tests()
    reset_db()


@pytest.fixture
def model():
    return install(assembly, SessionScriptedModel())


def test_an_agent_turn_is_run_as_that_agent_not_as_jarvis(model):
    model.on("research").says("Three sources agree: the market grew 12% in 2025.")
    outcome = runner.run_agent("research", "How fast is the home espresso market growing?",
                               conversation_id="c1")

    assert outcome.status == "done"
    assert outcome.result.startswith("Three sources agree")
    [request] = model.requests_of("research")
    system = request["system"]
    assert system.startswith("You are Research & Intelligence")
    assert "Cross-check important claims" in system          # its doctrine
    assert "Nothing that spends money" in system              # the shared guardrails
    assert "You are Jarvis" not in system and "boss" not in system
    assert request["sessionId"] == "agent:research:c1"
    # The task arrives as the agent's message, saying who asked.
    assert request["messages"][-1]["text"].startswith("Task from Jarvis:")


def test_only_what_the_agents_access_allows_is_declared_or_callable(model):
    # get_time is a core tool, declared to Jarvis on every turn — but not Research's.
    model.on("research").calls_tool("get_time", {}).says("I couldn't check the time.")
    outcome = runner.run_agent("research", "What time is it?", conversation_id="c1")

    declared = model.requests_of("research")[0]["tools"]
    assert "look_it_up" in declared and "get_time" not in declared
    assert outcome.tools_used == ["get_time"]
    # The call itself was refused by the policy layer, not merely undeclared.
    tool_message = model.requests_of("research")[1]["messages"][-1]
    assert "not available for this task" in str(tool_message["toolResults"])


def test_an_agent_with_access_to_everything_declares_only_its_own_set(model):
    from jarvis.orchestrator.pipeline import DECLARATION_BUDGET

    model.on("operations").says("Done.")
    runner.run_agent("operations", "Check the files", conversation_id="c1")
    declared = set(model.requests_of("operations")[0]["tools"])
    registry = assembly.get_registry()
    everything = {s.name for s in registry.list() if not s.has_tag("job")}
    assert len(everything) > DECLARATION_BUDGET  # otherwise this proves nothing
    # `mode: all` permits everything, but is not a licence to send every declaration:
    # the core set plus the agent's own tools, the rest reachable via find_capability.
    core = {s.name for s in registry.list() if s.has_tag("core") and not s.has_tag("job")}
    own = {n for n in (*runner.OWN_TOOLS, runner.DELEGATE_TOOL) if registry.has(n)}
    assert declared == core | own
    assert declared < everything


def test_the_agents_model_pin_is_passed_through_and_the_default_is_jarvis_selection(model):
    store.update_agent("content", {"modelPin": "gemini-2.5-flash"})
    model.on("content").says("Draft ready.")
    model.on("strategy").says("Recommendation ready.")
    runner.run_agent("content", "Write a caption", conversation_id="c1")
    runner.run_agent("strategy", "Should I raise prices?", conversation_id="c1")
    assert model.requests_of("content")[0]["modelId"] == "gemini-2.5-flash"
    assert model.requests_of("strategy")[0]["modelId"] is None


def test_memory_access_is_honoured(model):
    memory_store.create_memory(category="Work", text="Runs a small espresso cart business",
                               importance=5)
    model.on("strategy").says("a")
    model.on("content").says("b")
    store.update_agent("content", {"memoryAccess": "none"})
    runner.run_agent("strategy", "espresso pricing", conversation_id="c1")
    runner.run_agent("content", "espresso pricing", conversation_id="c1")
    assert "espresso cart" in model.requests_of("strategy")[0]["system"]
    assert "espresso cart" not in model.requests_of("content")[0]["system"]


def test_a_disabled_agent_takes_no_work(model):
    store.update_agent("scout", {"enabled": False})
    with pytest.raises(runner.AgentUnavailable, match="turned off"):
        runner.run_agent("scout", "hunt", conversation_id="c1")
    with pytest.raises(runner.AgentUnavailable, match="no specialist called"):
        runner.run_agent("nobody-at-all", "hunt", conversation_id="c1")
    assert model.requests == []


def test_every_run_is_recorded_and_announced(model):
    seen = []
    stops = [bus.subscribe(EventType.AGENT_RUN_STARTED, lambda e: seen.append(e.type)),
             bus.subscribe(EventType.AGENT_RUN_FINISHED, lambda e: seen.append(e.type))]
    model.on("analytics").says("Conversion fell 3 points week on week.")
    try:
        outcome = runner.run_agent("analytics", "Why did sales drop?", conversation_id="c9")
    finally:
        for stop in stops:
            stop()

    run = store.get_run(outcome.run["id"])
    assert run["status"] == "done" and run["agentId"] == "analytics"
    assert run["requestedBy"] == "jarvis" and run["depth"] == 1
    assert run["rootRunId"] == run["id"] and run["modelId"] == "scripted"
    assert EventType.AGENT_RUN_STARTED in seen and EventType.AGENT_RUN_FINISHED in seen


def test_a_specialist_cannot_answer_its_own_approval(model):
    # create_artifact is MEDIUM risk: with a person present it must be asked.
    model.on("content").calls_tool("create_artifact", {
        "filename": "post.docx", "paragraphs": ["Hello"]})
    outcome = runner.run_agent("content", "Make the post a Word file", conversation_id="c1",
                               autonomy=Autonomy.INTERACTIVE)
    assert outcome.status == "awaiting_approval"
    assert outcome.approval and outcome.approval["capability"] == "create_artifact"
    assert outcome.as_result()["approval"]["id"] == outcome.approval["id"]
    # One model step: the run stopped at the question instead of going on.
    assert len(model.requests_of("content")) == 1


def test_an_agent_picks_up_its_own_earlier_work_after_a_restart(model):
    model.on("teacher").says("Quiz 1: what does a for loop do?")
    runner.run_agent("teacher", "Start teaching me Python loops", conversation_id="c1")
    conversation.reset_for_tests()  # the process restarted; in-memory sessions are gone
    model.on("teacher").says("Correct!")
    runner.run_agent("teacher", "My answer: it repeats code for each item", conversation_id="c1")
    texts = [m.get("text") for m in model.requests_of("teacher")[1]["messages"]]
    assert "Quiz 1: what does a for loop do?" in texts


def test_a_specialist_always_sees_what_the_operator_is_working_towards(model):
    memory_store.create_memory(category="Long-term Goals",
                               text="Wants to quit the day job and run the cart full time by 2027")
    memory_store.create_memory(category="Preferences", text="Likes oat milk")
    model.on("scout").says("hunted")
    runner.run_agent("scout", "Find me something worth my time", conversation_id="c1")
    system = model.requests_of("scout")[0]["system"]
    assert "run the cart full time by 2027" in system   # a goal, though no word matches
    assert "oat milk" not in system                     # an unrelated preference, not forced in
