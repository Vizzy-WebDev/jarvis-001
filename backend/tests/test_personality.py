"""The adaptive communication register (S7): floors, sticky style, and the
reaction-marker scanner — all pure, all deterministic, all testable with no
server and no model.

The property under test is not "the regex compiles" — it is the same one the
module's own header states: distress/serious-topic detection is deliberately
NARROW (a false positive costs tone, a false negative is backstopped by the
model's own read), and the scanner must never let a marker split across a
streamed chunk boundary leak into visible text or glue two words together.
"""

from __future__ import annotations

from jarvis import session, session_hooks
from jarvis.personality import (
    Floors,
    create_reaction_scanner,
    detect_floors,
    floors_section,
    read_style,
    strip_reaction_markers,
)


# --- floors: the truth table --------------------------------------------------

def test_distress_floor_fires_on_the_documented_phrases():
    assert detect_floors("I'm so overwhelmed right now").distress
    assert detect_floors("I can't keep doing this").distress
    assert detect_floors("I've wasted months on this").distress
    assert detect_floors("I don't know what I'm doing").distress
    # The filler-tolerant variant, found live: "such a" sat between "like" and
    # "failure" and didn't match the tighter first attempt.
    assert detect_floors("I feel like such a total failure").distress
    assert detect_floors("I'm such a failure").distress
    assert detect_floors("I'm really struggling with this").distress


def test_distress_floor_does_not_fire_on_heat_aimed_at_a_thing_not_a_self():
    """The floor is deliberately narrow: frustration at a bug wants help, not
    care, and treating it as the latter is condescending exactly when
    directness was wanted."""
    assert not detect_floors("this fucking build is broken again").distress
    assert not detect_floors("I am the failure point in this design").distress
    assert not detect_floors("the deploy failed again, ugh").distress


def test_serious_topic_floor_fires_on_the_documented_markers():
    assert detect_floors("should I dip into my life savings for this").serious_topic
    assert detect_floors("I need to skip my meds this week").serious_topic  # the live gap
    assert detect_floors("thinking about filing for divorce").serious_topic
    assert detect_floors("should I quit my job and start a business").serious_topic


def test_serious_topic_floor_does_not_fire_on_unrelated_use_of_the_same_words():
    assert not detect_floors("I have a good relationship with this codebase").serious_topic
    assert not detect_floors("let's fire up the dev server").serious_topic


def test_explicit_direct_and_playful_floors():
    assert detect_floors("give it to me straight").explicit_direct
    assert detect_floors("be blunt with me").explicit_direct
    assert detect_floors("don't sugarcoat it").explicit_direct

    assert detect_floors("lighten up a bit").explicit_playful
    assert detect_floors("have fun with this one").explicit_playful
    # The gap S7's audit found and fixed: filler tolerated between "just" and
    # the actual request.
    assert detect_floors("I just want to joke around today").explicit_playful


def test_devils_advocate_and_real_opinion_floors():
    floors = detect_floors("play devil's advocate on this plan")
    assert floors.explicit_devils_advocate

    floors = detect_floors("okay but what do you actually think")
    assert floors.real_opinion_requested


def test_no_text_fires_nothing():
    floors = detect_floors(None)
    assert floors == Floors(False, False, False, False, False, False)
    assert detect_floors("") == floors


# --- sticky style --------------------------------------------------------------

def test_explicit_direct_becomes_sticky_and_survives_an_unrelated_turn():
    session_id = "sticky-1"
    floors, sticky = read_style(session_id, "give it to me straight from now on")
    assert floors.explicit_direct
    assert sticky == "direct"

    # A later, unrelated turn carries no explicit request of its own — the
    # style should still be "direct" because it is sticky, not per-turn.
    floors2, sticky2 = read_style(session_id, "what's the weather like")
    assert not floors2.explicit_direct
    assert sticky2 == "direct"


def test_explicit_playful_replaces_a_previously_sticky_direct_style():
    session_id = "sticky-2"
    read_style(session_id, "be blunt with me")
    _, sticky = read_style(session_id, "okay actually lighten up")
    assert sticky == "playful"


def test_real_opinion_request_does_not_clear_a_sticky_direct_playful_style():
    """Devil's-advocate framing and the direct/playful axis are unrelated —
    asking for the real opinion clears the former, never the latter."""
    session_id = "sticky-3"
    read_style(session_id, "lighten up")
    _, sticky = read_style(session_id, "wait, what do you actually think")
    assert sticky == "playful"


def test_a_fresh_session_has_no_sticky_style():
    _, sticky = read_style("brand-new-session", "hello")
    assert sticky is None


def test_distress_overrides_a_sticky_direct_style_for_one_turn_then_reverts():
    """This turn's own distress floor should suppress the "stay direct" line
    without touching the sticky value itself — the next, calmer turn reverts
    automatically with no re-ask needed."""
    session_id = "sticky-4"
    read_style(session_id, "give it to me straight")

    floors_now, sticky_now = read_style(session_id, "I'm so overwhelmed right now")
    assert floors_now.distress
    assert sticky_now == "direct"  # unchanged by the override
    section_now = floors_section(floors_now, sticky_now)
    assert "distressed or vulnerable" in section_now
    assert "stay direct" not in section_now  # suppressed for this turn only

    floors_next, sticky_next = read_style(session_id, "anyway, back to the plan")
    assert not floors_next.distress
    assert sticky_next == "direct"
    section_next = floors_section(floors_next, sticky_next)
    assert "stay direct" in section_next  # reverted, with no re-ask


def test_serious_topic_overrides_a_sticky_playful_style_for_one_turn_then_reverts():
    session_id = "sticky-5"
    read_style(session_id, "have fun with this")

    floors_now, sticky_now = read_style(session_id, "should I file for divorce")
    section_now = floors_section(floors_now, sticky_now)
    assert "measured, low on playfulness" in section_now
    assert "keep it light" not in section_now

    floors_next, sticky_next = read_style(session_id, "what should I have for lunch")
    section_next = floors_section(floors_next, sticky_next)
    assert "keep it light" in section_next


def test_floors_section_is_empty_when_nothing_fired():
    assert floors_section(detect_floors("what time is it"), None) == ""


def test_floors_section_of_none_is_empty():
    assert floors_section(None, "direct") == ""


# --- session cleanup, through the real reset path -----------------------------

def test_a_new_chat_clears_the_sticky_style_through_the_real_reset_path(scratch):
    """Not a direct call to `personality.clear_session()` — the property this
    protects is that `session.reset_conversation()` itself reaches it, via the
    same `session_hooks` registry the other Wave-2 modules use."""
    assert "personality" in session_hooks.registered_names()

    session_id = session.get_active_session_id()
    read_style(session_id, "give it to me straight from now on")
    _, sticky = read_style(session_id, "anything else")
    assert sticky == "direct"

    session.reset_conversation()

    # The OLD session's sticky style must not survive; re-reading it directly
    # (rather than trusting a fresh session id to merely look clean) proves the
    # hook actually ran rather than the state just being unreachable.
    _, sticky_after = read_style(session_id, "anything else")
    assert sticky_after is None


# --- the reaction-marker scanner ------------------------------------------------

def _drain(scanner, text):
    events = []
    for ch in text:
        events.extend(scanner.feed(ch))
    events.extend(scanner.flush())
    return events


def _reconstruct(events):
    return "".join(e.text for e in events if e.type == "text")


def test_scanner_extracts_a_marker_fed_all_at_once():
    scanner = create_reaction_scanner()
    events = scanner.feed("that's hilarious [[laugh]] okay anyway")
    events += scanner.flush()
    kinds = [e.type for e in events]
    assert "reaction" in kinds
    reaction = next(e for e in events if e.type == "reaction")
    assert reaction.kind == "laugh"
    # The marker's own preceding space is swallowed; the following one is not.
    assert _reconstruct(events) == "that's hilarious okay anyway"


def test_scanner_extracts_a_marker_fed_character_by_character():
    """The actual property S7 needs: some adapters stream a token at a time,
    so a marker can arrive split across an arbitrary number of feed() calls."""
    scanner = create_reaction_scanner()
    events = _drain(scanner, "that's hilarious [[laugh]] okay anyway")
    reactions = [e for e in events if e.type == "reaction"]
    assert len(reactions) == 1
    assert reactions[0].kind == "laugh"
    assert _reconstruct(events) == "that's hilarious okay anyway"


def test_scanner_never_fires_on_text_that_only_resembles_a_marker():
    scanner = create_reaction_scanner()
    events = _drain(scanner, "he said [[laughing]] out loud, not [[laugh")
    assert not any(e.type == "reaction" for e in events)
    assert _reconstruct(events) == "he said [[laughing]] out loud, not [[laugh"


def test_scanner_never_fires_more_than_once_even_with_two_markers():
    """STYLE_FRAMEWORK tells the model never more than once per reply; the
    scanner itself still handles a second one mechanically rather than
    silently dropping it, since it must not depend on the model obeying."""
    scanner = create_reaction_scanner()
    events = _drain(scanner, "[[laugh]] and later [[laugh]] again")
    reactions = [e for e in events if e.type == "reaction"]
    assert len(reactions) == 2
    # The space FOLLOWING each marker is deliberately preserved (only a
    # marker's own PRECEDING space is swallowed), so the leading space on
    # " and later" survives from after the first marker.
    assert _reconstruct(events) == " and later again"


def test_strip_reaction_markers_on_a_complete_string():
    assert strip_reaction_markers("that's funny [[laugh]] okay") == "that's funny okay"
    assert strip_reaction_markers("[[laugh]]") == ""
    assert strip_reaction_markers("") == ""
    assert strip_reaction_markers(None) is None


def test_strip_reaction_markers_matches_the_streamed_paths_whitespace_rule():
    """Built on the same scanner as the streamed path — must not drift from
    it. Only the marker's own preceding space is swallowed."""
    streamed = create_reaction_scanner()
    events = streamed.feed("wow [[laugh]] really") + streamed.flush()
    assert _reconstruct(events) == strip_reaction_markers("wow [[laugh]] really")
