"""Reasoning a server writes INTO the reply text (`<think>…`) is taken back out — without ever
losing the answer, however the text is chunked, and with streaming and tool calls intact.

The shapes are the ones measured on the person's own provider: closed thinking; the closing tag
dropped when tools are declared (only blank lines left); thinking and then a tool call; a
reply that is nothing but unclosed thinking.
"""

from __future__ import annotations

import pytest

from jarvis.models.drivers._inline_think import InlineThinking
from jarvis.orchestrator.model_port import StepComplete, TextChunk

from stub_wire import StubWire, Turn
from test_models import add, client, run_step, serve  # noqa: F401 — fixtures


def split(chunks: list[str], *, tools: bool = False) -> tuple[str, str]:
    """(reasoning, text) after feeding `chunks` in order."""
    thinking, out = InlineThinking(), []
    for chunk in chunks:
        out += thinking.feed(chunk)
    out += thinking.finish(called_tools=tools)
    return ("".join(t for kind, t in out if kind == "reasoning"),
            "".join(t for kind, t in out if kind == "text"))


def every_chunking(text: str, *, tools: bool = False) -> set[tuple[str, str]]:
    seen = {split([text], tools=tools), split(list(text), tools=tools)}
    for a in range(len(text) + 1):
        for b in range(a, len(text) + 1):
            seen.add(split([text[:a], text[a:b], text[b:]], tools=tools))
    return seen


CASES = [
    # name, what the server sent, tool called?, (reasoning, text)
    ("an ordinary reply is untouched",
     "Hello there.\n\n\nStill the answer </think> ok", False,
     ("", "Hello there.\n\n\nStill the answer </think> ok")),
    ("closed thinking", "<think>I should greet.\n</think>\n\nHello, Boss.", False,
     ("I should greet.\n", "Hello, Boss.")),
    ("the closing tag dropped, only the blank lines left",
     "<think>I should greet.\n\n\nHello, Boss.\n\n\nMore.", True,
     ("I should greet.", "Hello, Boss.\n\n\nMore.")),
    ("thinking, then a tool call", "<think>Read the next one.\n", True, ("Read the next one.\n", "")),
    ("nothing but unclosed thinking is handed back as the reply",
     "<think>The total is 42.\n", False, ("The total is 42.", "The total is 42.")),
    ("a reply that only MENTIONS the tag", "Use the <think> tag like so: <think>x</think>", False,
     ("", "Use the <think> tag like so: <think>x</think>")),
    ("something that only looks like the tag", "<thinking about it> yes", False,
     ("", "<thinking about it> yes")),
    ("whitespace before the tag", "\n <think>x</think>y", False, ("x", "y")),
    ("a reply that ends part-way through the tag", "<thi", False, ("", "<thi")),
    ("an empty reply", "", False, ("", "")),
    ("extra blank lines after the thinking", "<think>x</think>\n\n\n\nAnswer", False, ("x", "Answer")),
]


@pytest.mark.parametrize("name,sent,tools,want", CASES, ids=[c[0] for c in CASES])
def test_each_shape_comes_out_the_same_however_it_is_chunked(name, sent, tools, want):
    assert every_chunking(sent, tools=tools) == {want}


def test_the_answer_is_never_swallowed_when_the_thinking_itself_holds_a_gap():
    """The one shape with two readings: a gap inside thinking that is closed later. Which
    reading wins depends on what has arrived — but the answer is in the text either way."""
    for reasoning, text in every_chunking("<think>a\n\n\nb</think>Hi"):
        assert text.endswith("Hi") and reasoning.startswith("a")


def test_thinking_streams_as_it_arrives_rather_than_all_at_the_end():
    thinking = InlineThinking()
    assert thinking.feed("<think>First I will ") == [("reasoning", "First I will ")]
    assert thinking.feed("look.</think>") == [("reasoning", "look.")]
    assert thinking.feed("Here ") == [("text", "Here ")] and thinking.feed("it is.") == [("text", "it is.")]


# --- through the real driver, over a real HTTP stream ----------------------------------------

TOOL = {"name": "get_time", "description": "Tell the time.", "parameters": {"type": "object", "properties": {}}}


def final(events) -> StepComplete:
    return next(e for e in events if isinstance(e, StepComplete))


def spoken(events) -> str:
    return "".join(e.text for e in events if isinstance(e, TextChunk))


@pytest.mark.parametrize("piece", [0, 1, 3, 7])
def test_thinking_never_reaches_the_reply_or_the_stream(client, serve, piece):
    stub = serve("openai_chat")
    add(client, stub)
    stub.queue(Turn(text="<think>They greeted me. I should greet back.\n\n\nGood morning, Boss.", piece=piece))
    events, error = run_step(tools=[TOOL])
    assert error is None
    assert final(events).text == "Good morning, Boss." and spoken(events) == "Good morning, Boss."

    stub.queue(Turn(text="<think>They greeted me.\n</think>\n\nGood morning, Boss.", piece=piece))
    events, error = run_step()
    assert final(events).text == "Good morning, Boss." and spoken(events) == "Good morning, Boss."


def test_thinking_before_a_tool_call_leaves_the_call_whole_and_no_words(client, serve):
    stub = serve("openai_chat")
    add(client, stub)
    stub.queue(Turn(text="<think>I need the time for this.\n", tools=[("get_time", {})], say_with_tools=True))
    events, error = run_step(tools=[TOOL])
    done = final(events)
    assert error is None and done.text == "" and spoken(events) == ""
    assert [(c.name, c.args) for c in done.tool_calls] == [("get_time", {})]


def test_a_reply_that_is_all_unclosed_thinking_is_not_lost(client, serve):
    stub = serve("openai_chat")
    add(client, stub)
    stub.queue(Turn(text="<think>The total is 42.\n"))
    events, error = run_step(tools=[TOOL])
    assert error is None and final(events).text == "The total is 42."


def test_an_ordinary_reply_and_its_streaming_are_unchanged(client, serve):
    stub = serve("openai_chat")
    add(client, stub)
    stub.queue(Turn(text="To show reasoning, some models write <think> first.\n\n\nThat is all."))
    events, error = run_step()
    assert final(events).text == "To show reasoning, some models write <think> first.\n\n\nThat is all."
    assert len([e for e in events if isinstance(e, TextChunk)]) > 3        # still word by word


def test_a_server_can_be_told_to_leave_the_text_alone(client, serve):
    from jarvis.models import config

    stub = serve("openai_chat")
    connection = add(client, stub)["connection"]["id"]
    doc = config.raw()
    doc.setdefault("quirk_profiles", {})["as_sent"] = {"wire": {"keep_inline_thinking": True}}
    for conn in doc["connections"]:
        if conn["name"] == connection:
            conn["quirks"] = "as_sent"
    config.save(doc)
    stub.queue(Turn(text="<think>x</think>Hello."))
    events, error = run_step()
    assert final(events).text == "<think>x</think>Hello."
