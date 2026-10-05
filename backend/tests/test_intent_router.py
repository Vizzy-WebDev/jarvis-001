"""Intent routing (§11) and the fast path (§10).

Most of these test what the router REFUSES to shortcut. A deterministic fast path
that swallows a nuanced request produces a confidently wrong answer; falling
through to the model merely costs a second. The bias is deliberate and asserted.
"""

from __future__ import annotations

import pytest

from jarvis.intent import Intent, classify


# --- the fast path fires on exactly what it should --------------------------

@pytest.mark.parametrize("text,capability", [
    ("What's the time?", "get_time"),
    ("what time is it", "get_time"),
    ("the time", "get_time"),
    ("What's today's date?", "get_time"),
    ("what day is it", "get_time"),
    ("Open Chrome.", "open_app"),
    ("launch vscode", "open_app"),
    ("please start Spotify", "open_app"),
    ("turn the volume down", "set_volume"),
    ("volume up", "set_volume"),
    ("mute", "set_volume"),
    ("What's my battery level?", "system_info"),
    ("battery", "system_info"),
])
def test_simple_requests_take_the_fast_path(text, capability):
    """§10's own examples must never need a model call."""
    route = classify(text)
    assert route.is_fast, f"{text!r} should have been fast-pathed"
    assert route.fast_path.capability == capability
    assert route.confidence == 1.0


def test_the_fast_path_carries_usable_arguments():
    assert classify("Open Chrome").fast_path.args == {"name": "Chrome"}
    assert classify("volume down").fast_path.args == {"direction": "down"}
    assert classify("what's the date").fast_path.args == {"include_date": True}


# --- and refuses everything that carries more meaning -----------------------

@pytest.mark.parametrize("text", [
    "What's the time in Tokyo?",              # a qualifier changes the answer
    "what time is it and what's the date",    # two things
    "open Chrome and find the doc",           # a second action
    "why is my battery draining",             # wants reasoning, not a reading
    "remind me every day at 9",               # a rule, not an act
    "what's the time tomorrow",               # not now
    "if the volume is low turn it up",        # conditional
    "open Chrome when I get back",            # deferred
    "compare my battery to yesterday",        # comparison
    "what's the time? what's the date?",      # two questions
])
def test_anything_carrying_more_meaning_falls_through_to_the_model(text):
    route = classify(text)
    assert not route.is_fast, f"{text!r} was wrongly shortcut"


def test_a_topic_that_looks_like_an_app_is_not_launched():
    """"open the discussion" is not a launch request."""
    for text in ("open the discussion", "open that", "open it", "open a case"):
        assert not classify(text).is_fast, f"{text!r} was treated as an app launch"


def test_an_article_is_stripped_but_a_real_app_still_launches():
    """"open the calculator" is still a launch; the article is not the name."""
    route = classify("open the calculator")
    assert route.is_fast
    assert route.fast_path.args == {"name": "calculator"}


def test_one_topic_word_disqualifies_the_whole_phrase():
    """The guard is token-wise, so a topic word cannot hide behind a real one."""
    assert not classify("open the settings discussion").is_fast


def test_an_article_with_nothing_after_it_is_not_a_launch():
    assert not classify("open the").is_fast


def test_a_long_utterance_is_never_fast_pathed():
    route = classify("could you please go ahead and open up Chrome for me now")
    assert not route.is_fast
    assert "longer than a simple command" in route.reason


# --- honest classification for the rest --------------------------------------

def test_empty_input_asks_for_clarification():
    assert classify("").intent is Intent.CLARIFY
    assert classify("   ").intent is Intent.CLARIFY


@pytest.mark.parametrize("text,intent", [
    ("remember that I prefer short answers", Intent.MEMORY),
    ("what do you remember about this project", Intent.MEMORY),
    ("research the top three competitors", Intent.RESEARCH),
    ("look into database options", Intent.RESEARCH),
    ("keep working on that in the background", Intent.BACKGROUND_JOB),
])
def test_hints_are_offered_for_the_harder_categories(text, intent):
    route = classify(text)
    assert route.intent is intent


def test_hints_are_low_confidence_and_say_so():
    """§45: a regex cannot reliably tell RESEARCH from MULTI_STEP, and pretending
    otherwise would be fake intelligence. The hint must announce its own
    uncertainty so the orchestrator lets the model settle it."""
    route = classify("research the competitors")
    assert route.confidence < 0.5
    assert "model should confirm" in route.reason
    assert not route.is_fast


def test_ordinary_conversation_is_chat_with_no_confidence():
    route = classify("I've been thinking about how to structure this project")
    assert route.intent is Intent.CHAT
    assert route.confidence == 0.0
    assert not route.is_fast


def test_every_route_explains_itself():
    """A routing decision the user or a log cannot account for is not usable."""
    for text in ("what's the time", "research X", "hello there", "",
                 "what's the time in Tokyo"):
        assert classify(text).reason


# --- the module's own discipline ---------------------------------------------

def test_the_router_is_pure():
    """No model call, no I/O, no state — so the whole rule set stays exercisable
    as a truth table, which is the pattern that actually holds up here."""
    import inspect

    from jarvis.intent import router

    source = inspect.getsource(router)
    for forbidden in ("import requests", "httpx", "get_db(", "askModel", "ask_model", "open("):
        assert forbidden not in source, f"the router reached for {forbidden}"


def test_classification_is_stable():
    """The same utterance must route the same way every time."""
    for text in ("what's the time", "open Chrome", "research the market"):
        first, second = classify(text), classify(text)
        assert (first.intent, first.confidence, first.is_fast) == \
               (second.intent, second.confidence, second.is_fast)
