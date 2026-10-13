"""Words that reached the person are never lost or rewritten: a reply cut off by a failure keeps its
words and the error is its own thing, and an interruption of a new reply never touches the one before."""

from __future__ import annotations

import threading

import pytest

from jarvis import conversation
from jarvis.capabilities import CapabilityRegistry
from jarvis.db import reset_for_tests as reset_db
from jarvis.events.bus import EventBus
from jarvis.orchestrator import Chunk, Done, Failed, Interrupted, Orchestrator, StepComplete, TextChunk, TurnRequest


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    conversation.reset_for_tests()
    yield
    conversation.reset_for_tests()
    reset_db()


class Scripted:
    def __init__(self, *turns):
        self.turns = list(turns)

    def stream(self, **_):
        return self.turns.pop(0)()


def _say(text):
    def go():
        yield TextChunk(text)
        yield StepComplete(text=text, model_id="stub")
    return go


def _run(orch, text, cancel=None):
    return list(orch.run_turn(TurnRequest(text=text, session_id="s1"), cancel))


def test_a_failure_after_words_were_streamed_keeps_the_words_and_reports_the_error_beside_them():
    def breaks():
        yield TextChunk("Here is the start of ")
        yield TextChunk("the answer")
        raise RuntimeError("the connection dropped")

    orch = Orchestrator(Scripted(breaks), registry=CapabilityRegistry(), event_bus=EventBus())
    events = _run(orch, "tell me")

    assert "".join(e.text for e in events if isinstance(e, Chunk)) == "Here is the start of the answer"
    assert isinstance(events[-1], Failed)
    saved = [m for m in conversation.get_messages("s1") if m["role"] == "assistant"]
    assert [m["text"] for m in saved] == ["Here is the start of the answer"]
    assert saved[0]["unfinished"] is True and not saved[0].get("interrupted")


def test_a_failure_before_any_words_saves_no_empty_reply():
    def breaks():
        raise RuntimeError("down")
        yield  # pragma: no cover

    orch = Orchestrator(Scripted(breaks), registry=CapabilityRegistry(), event_bus=EventBus())
    assert isinstance(_run(orch, "tell me")[-1], Failed)
    assert [m for m in conversation.get_messages("s1") if m["role"] == "assistant"] == []


def test_interrupting_a_new_reply_never_rewrites_the_one_before_it():
    cancel = threading.Event()

    def cut():
        yield TextChunk("Second, but cut ")
        cancel.set()
        yield TextChunk("and never heard.")
        yield StepComplete(text="x", model_id="stub")

    orch = Orchestrator(Scripted(_say("First answer, complete."), cut), registry=CapabilityRegistry(),
                        event_bus=EventBus())
    assert isinstance(_run(orch, "one")[-1], Done)
    events = _run(orch, "two", cancel)
    assert isinstance(events[-1], Interrupted) and events[-1].spoken_text == "Second, but cut "

    assistant = [m for m in conversation.get_messages("s1") if m["role"] == "assistant"]
    assert len(assistant) == 2
    assert not assistant[0].get("interrupted") and assistant[0]["text"] == "First answer, complete."
    assert assistant[1]["interrupted"] is True and assistant[1]["spokenText"] == "Second, but cut "


def test_a_late_interrupt_with_no_new_reply_yet_never_marks_the_previous_turns_reply():
    conversation._push("s", {"role": "user", "text": "first"})
    conversation._push("s", {"role": "assistant", "text": "old answer"})
    conversation._push("s", {"role": "user", "text": "second"})
    assert conversation.mark_last_assistant_interrupted("s", "heard") is False
    old = [m for m in conversation._sessions["s"] if m["role"] == "assistant"][0]
    assert "interrupted" not in old and "spokenText" not in old
