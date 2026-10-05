"""Conversation memory: a context budget that is the answering model's own, and a running
summary of what scrolled out.

Everything model-facing here runs through the REAL model layer with the scripted `fake`
driver (`tests/layer_helpers.py`): the size a turn gets is read from what each fake model
declares, the refusal a model sends back is a real `context_too_long`, and the summary is a
real structured-output call. No test here contains a fixed context size, because the code
under test must not have one either.
"""

from __future__ import annotations

import json

import pytest

from jarvis import background, chat_store, conversation, conversation_summary
from jarvis.capabilities import CapabilityRegistry
from jarvis.db import reset_for_tests as reset_db
from jarvis.events.bus import Event, EventBus
from jarvis.events import EventType
from jarvis.models import errors, state
from jarvis.models.client import JarvisModelClient
from jarvis.models.drivers import fake
from jarvis.orchestrator import Orchestrator, TurnRequest
from jarvis.orchestrator.context import (
    RelevanceContext, budget_for, fit_messages, message_cost, shorten_old_tool_results,
)
from jarvis.orchestrator.pipeline import Done, Failed
from layer_helpers import CHAT, configure, fake_conn, layer  # noqa: F401 — `layer` is a fixture


@pytest.fixture(autouse=True)
def _isolate(layer):  # noqa: F811
    reset_db()
    conversation.reset_for_tests()
    yield
    background.join_all()
    conversation.reset_for_tests()
    reset_db()


def model(context: int | None = None, **caps):
    entry = {"capabilities": {**CHAT, **caps}}
    if context is not None:
        entry["context"] = context
    return {"m": entry}


def notes(**overrides):
    base = {"goal": "", "facts": [], "decisions": [], "open": [], "parked": [], "current": ""}
    return json.dumps({**base, **overrides})


def chat(session: str, turns: int, words: int = 40) -> None:
    for i in range(turns):
        conversation.push_user_text(session, f"question {i} " + "detail " * words)
        conversation.push_assistant_text(session, f"answer {i} " + "reply " * words)


def run(orchestrator, session="s1", text="and now?"):
    return list(orchestrator.run_turn(TurnRequest(text=text, session_id=session)))


def orchestrator():
    return Orchestrator(JarvisModelClient(), registry=CapabilityRegistry(), event_bus=EventBus())


# --- the budget is the answering model's --------------------------------------------------

def test_the_budget_scales_with_the_model_and_is_none_when_it_never_said():
    small, large = budget_for(8_000), budget_for(200_000)
    assert 0 < small < large
    assert budget_for(None) is None and budget_for(0) is None
    # A declared maximum output is room kept for the reply…
    assert budget_for(200_000, output_tokens=8_000) > budget_for(200_000)
    # …and the tool declarations sent with a step come out of it too.
    assert budget_for(8_000, tool_chars=4_000) < small


def test_capacity_is_read_from_the_model_that_would_answer(layer):  # noqa: F811
    configure(layer, [fake_conn("small", models=model(8_000)), fake_conn("big", models=model(200_000)),
                      fake_conn("quiet", models=model())],
              aliases={"selected": {"endpoint": "small/m"}})
    assert JarvisModelClient().context_capacity(session_id="s1") == (8_000, None)

    configure(layer, [fake_conn("small", models=model(8_000)), fake_conn("big", models=model(200_000))],
              aliases={"selected": {"endpoint": "big/m"}})
    assert JarvisModelClient().context_capacity(session_id="s1") == (200_000, None)

    configure(layer, [fake_conn("quiet", models=model())], aliases={"selected": {"endpoint": "quiet/m"}})
    assert JarvisModelClient().context_capacity(session_id="s1") == (None, None)


def test_a_small_model_s_turn_fits_it_and_a_big_one_keeps_everything(layer):  # noqa: F811
    chat("s1", 60, words=150)
    configure(layer, [fake_conn("small", models=model(8_000))], aliases={"selected": {"endpoint": "small/m"}})
    events = run(orchestrator())
    assert isinstance(events[-1], Done), events[-1]
    small_items = len(fake.calls("small")[-1].prepared.items)

    configure(layer, [fake_conn("big", models=model(1_000_000))], aliases={"selected": {"endpoint": "big/m"}})
    assert isinstance(run(orchestrator(), text="and then?")[-1], Done)
    big_items = len(fake.calls("big")[-1].prepared.items)
    # The small model's own context check was never tripped by Jarvis's assembly, and the
    # large one got the conversation the small one had no room for.
    assert small_items < big_items


def test_a_model_that_never_said_its_size_gets_no_limit_from_jarvis(layer):  # noqa: F811
    chat("s1", 25, words=150)
    configure(layer, [fake_conn("quiet", models=model())], aliases={"selected": {"endpoint": "quiet/m"}})
    assert isinstance(run(orchestrator())[-1], Done)
    sent = fake.calls("quiet")[-1].prepared.items
    assert len(sent) == len(conversation.get_messages("s1")) - 1  # everything, minus the reply just made


# --- learning a window from the model's own refusal ---------------------------------------

def test_a_refusal_as_too_long_is_learned_and_the_step_is_refitted(layer):  # noqa: F811
    chat("s1", 25, words=150)
    configure(layer, [fake_conn("quiet", models=model())], aliases={"selected": {"endpoint": "quiet/m"}})
    fake.queue("quiet", errors.ContextTooLong("too long for this model"), fake.reply("fits now"))

    events = run(orchestrator())
    assert isinstance(events[-1], Done) and events[-1].text == "fits now"
    first, second = fake.calls("quiet")[-2:]
    assert len(second.prepared.items) < len(first.prepared.items)
    learned = state.probed()["quiet/m"]["max_context_tokens"]
    assert JarvisModelClient().context_capacity(session_id="s1")[0] == learned

    # The next turn starts from what was learned: no refusal needed to fit.
    fake.queue("quiet", fake.reply("still fine"))
    assert isinstance(run(orchestrator(), text="next")[-1], Done)


def test_a_second_refusal_in_one_turn_fails_it_plainly(layer):  # noqa: F811
    chat("s1", 25, words=150)
    configure(layer, [fake_conn("quiet", models=model())], aliases={"selected": {"endpoint": "quiet/m"}})
    fake.queue("quiet", errors.ContextTooLong("too long"), errors.ContextTooLong("still too long"))
    events = run(orchestrator())
    assert isinstance(events[-1], Failed)
    assert len(fake.calls("quiet")) == 2


def test_a_learned_window_only_ever_shrinks():
    assert state.record_learned_context("x/m", 5_000) == 5_000
    assert state.record_learned_context("x/m", 9_000) == 5_000
    assert state.record_learned_context("x/m", 3_000) == 3_000


# --- what gives way when the budget is tight ----------------------------------------------

def _web_turn(big: str):
    return [{"role": "user", "text": "look it up"},
            {"role": "assistant", "toolCalls": [{"id": "1", "name": "read_web_page", "args": {}}]},
            {"role": "tool", "toolResults": [{"id": "1", "name": "read_web_page", "result": big}]},
            {"role": "assistant", "text": "found it"}]


def test_an_old_web_result_is_shortened_before_the_conversation_is_dropped():
    """The measured bug: one large web result pushed everything said before it out."""
    early = [{"role": "user", "text": "my budget is 400 pounds"}, {"role": "assistant", "text": "noted"}]
    current = [{"role": "user", "text": "so which one fits?"}]
    history = early + _web_turn("x" * 40_000) + current
    budget = sum(message_cost(m) for m in early + current) + 400

    kept = fit_messages(history, budget)
    assert kept[0]["text"] == "my budget is 400 pounds"
    shortened = next(m for m in kept if m["role"] == "tool")["toolResults"][0]["result"]
    assert "shortened" in shortened and len(shortened) < 1_000


def test_room_to_spare_changes_nothing_and_the_current_turn_s_result_is_never_cut():
    history = _web_turn("y" * 5_000)
    assert fit_messages(history, None) == history
    assert fit_messages(history, 10**9) == history
    current = shorten_old_tool_results(history)  # no later user message: it IS the current turn
    assert current[2]["toolResults"][0]["result"] == "y" * 5_000


# --- the running summary -------------------------------------------------------------------

def _persisted(turns: int, words: int = 60) -> str:
    conv = chat_store.create_conversation()["id"]
    conversation.bind_session(conv)
    chat(conv, turns, words)
    return conv


def test_live_and_reloaded_messages_carry_their_saved_seq():
    conv = _persisted(2)
    live = conversation.get_messages(conv)
    assert [m["seq"] for m in live] == [1, 2, 3, 4]
    conversation.reset_for_tests()
    conversation.hydrate(conv)
    assert [m["seq"] for m in conversation.get_messages(conv)] == [1, 2, 3, 4]


def test_folding_is_proportional_to_the_model_and_never_takes_the_latest_turns():
    conv = _persisted(10)
    messages = chat_store.get_messages_since(conv, 0)
    total = sum(message_cost(m) for m in messages)

    assert conversation_summary.plan_fold(messages, total * 4) == []  # plenty of room: nothing
    small = conversation_summary.plan_fold(messages, total // 2)
    smaller = conversation_summary.plan_fold(messages, total // 4)
    assert small and len(smaller) >= len(small)
    # Whole turns, oldest first, and the last two exchanges stay word for word.
    assert small[0]["seq"] == 1 and small[-1]["role"] == "assistant"
    assert small[-1]["seq"] <= messages[-5]["seq"]


def test_with_no_known_size_only_what_leaves_the_working_set_is_folded():
    conv = _persisted(40)  # 80 messages; the in-memory working set holds fewer
    messages = chat_store.get_messages_since(conv, 0)
    fold = conversation_summary.plan_fold(messages, None)
    assert fold and len(fold) <= len(messages) - conversation.MAX_HISTORY_ENTRIES
    assert conversation_summary.plan_fold(messages[:20], None) == []


def test_refresh_writes_structured_notes_and_carries_them_forward(layer):  # noqa: F811
    configure(layer, [fake_conn("a", models=model())])
    conv = _persisted(10)
    budget = sum(message_cost(m) for m in chat_store.get_messages_since(conv, 0)) // 4

    fake.queue("a", fake.reply(notes(goal="plan the trip", decisions=["Lisbon in May"])))
    assert conversation_summary.refresh(conv, budget)
    stored = conversation_summary.get(conv)
    assert stored["summary"]["goal"] == "plan the trip"
    first_cover = stored["coveredSeq"]
    assert chat_store.get_messages_since(conv, first_cover - 1)[0]["role"] == "assistant"

    chat(conv, 10)
    fake.queue("a", fake.reply(notes(goal="plan the trip", decisions=["Lisbon in May", "fly Tuesday"])))
    assert conversation_summary.refresh(conv, budget)
    sent = json.dumps([str(i) for i in fake.calls("a")[-1].prepared.items])
    assert "Lisbon in May" in sent  # the previous notes went in, to be carried forward
    assert conversation_summary.get(conv)["coveredSeq"] > first_cover


def test_assembly_uses_the_summary_in_place_of_what_it_covers(layer):  # noqa: F811
    configure(layer, [fake_conn("a", models=model())])
    conv = _persisted(10)
    budget = sum(message_cost(m) for m in chat_store.get_messages_since(conv, 0)) // 4
    fake.queue("a", fake.reply(notes(goal="choose a laptop", open=["send the shortlist"])))
    assert conversation_summary.refresh(conv, budget)
    covered = conversation_summary.get(conv)["coveredSeq"]

    context = RelevanceContext().assemble(session_id=conv, text="which one?")
    assert "choose a laptop" in context.system and "send the shortlist" in context.system
    assert all(m["seq"] > covered for m in context.messages)
    assert context.messages[-1]["text"].startswith("answer 9")
    assert context.notes["summaryCoveredSeq"] == covered


def test_edit_or_retry_into_the_summarized_part_drops_the_summary(layer):  # noqa: F811
    configure(layer, [fake_conn("a", models=model())])
    conv = _persisted(10)
    budget = sum(message_cost(m) for m in chat_store.get_messages_since(conv, 0)) // 4
    fake.queue("a", fake.reply(notes(goal="g")))
    assert conversation_summary.refresh(conv, budget)
    covered = conversation_summary.get(conv)["coveredSeq"]
    messages = chat_store.get_messages_since(conv, 0)

    after = next(m for m in messages if m["seq"] > covered)
    chat_store.truncate_to_before(conv, after["id"])
    assert conversation_summary.get(conv) is not None  # a cut after what it covers keeps it

    inside = next(m for m in messages if m["seq"] == covered)
    chat_store.truncate_to_before(conv, inside["id"])
    assert conversation_summary.get(conv) is None


def test_no_model_or_unusable_notes_store_nothing(layer):  # noqa: F811
    configure(layer, [])
    conv = _persisted(10)
    assert conversation_summary.refresh(conv, 100) is False
    configure(layer, [fake_conn("a", models=model())])
    fake.queue("a", fake.reply(json.dumps({"facts": "not a list"})))
    assert conversation_summary.refresh(conv, 100) is False
    assert conversation_summary.get(conv) is None


def test_the_observer_only_runs_behind_its_interlock(layer, monkeypatch):  # noqa: F811
    configure(layer, [fake_conn("a", models=model())])
    conv = _persisted(10)
    budget = sum(message_cost(m) for m in chat_store.get_messages_since(conv, 0)) // 4
    reply = Event(EventType.ASSISTANT_RESPONSE, {"sessionId": conv, "contextBudget": budget})

    monkeypatch.delenv(conversation_summary.ENABLE_ENV, raising=False)
    conversation_summary.on_reply(reply)
    background.join_all()
    assert conversation_summary.get(conv) is None

    monkeypatch.setenv(conversation_summary.ENABLE_ENV, "1")
    fake.queue("a", fake.reply(notes(goal="g")))
    conversation_summary.on_reply(reply)
    background.join_all()
    assert conversation_summary.get(conv)["summary"]["goal"] == "g"


# --- the removed features stay removed ---------------------------------------------------

def test_past_chat_search_and_track_goal_are_gone():
    from jarvis.agents.builtins import BUILTIN_AGENTS
    from jarvis.jobs.worker import TOOLS_BY_KIND
    from jarvis.tools import load_tools

    names = set(load_tools(CapabilityRegistry()))
    assert "search_conversations" not in names and "track_goal" not in names
    assert "check_myself" in names
    assert not any("search_conversations" in (tools or []) for tools in TOOLS_BY_KIND.values())
    assert not any("search_conversations" in a["capabilityAccess"].get("names", []) for a in BUILTIN_AGENTS)
