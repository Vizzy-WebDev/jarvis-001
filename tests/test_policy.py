"""The permission decision (§7) and its floors (§8).

Written as an exhaustive truth table: a pure decision function whose safety floors are
asserted rather than described.
"""

from __future__ import annotations

import itertools
import time

import pytest

from jarvis.capabilities import CapabilitySpec, Risk
from jarvis.policy import Autonomy, CallContext, Grant, Outcome, Surface, decide


def spec(name="act", risk=Risk.LOW) -> CapabilitySpec:
    return CapabilitySpec(
        id=f"builtin.{name}", name=name, description="", input_schema={},
        risk=risk, handler=lambda **_: None,
    )


def ctx(**kw) -> CallContext:
    defaults = dict(
        session_id="s1", turn_id="t1",
        surface=Surface.TEXT, autonomy=Autonomy.INTERACTIVE,
    )
    defaults.update(kw)
    return CallContext(**defaults)


# --- the context itself is the structural fix -------------------------------

def test_context_cannot_be_constructed_without_a_turn_id():
    """The Live voice path calls invoke() with no context at all, so the
    same-turn approval bypass cannot be detected there. Making turn_id required
    means that call site fails loudly instead of losing its protections."""
    with pytest.raises(ValueError, match="turn id"):
        CallContext(session_id="s1", turn_id="", surface=Surface.VOICE,
                    autonomy=Autonomy.INTERACTIVE)


# --- floors ------------------------------------------------------------------

def test_voice_is_never_a_bypass():
    """§8: "A dangerous action should never bypass the gate simply because it
    originated from voice mode." Asserted across every surface."""
    for surface in Surface:
        result = decide(spec(risk=Risk.HIGH), ctx(surface=surface))
        assert result.outcome is Outcome.NEEDS_APPROVAL, f"{surface.value} bypassed the gate"


def test_high_risk_always_needs_a_human_whatever_the_autonomy():
    for autonomy in Autonomy:
        result = decide(spec(risk=Risk.HIGH), ctx(autonomy=autonomy))
        assert result.outcome is Outcome.NEEDS_APPROVAL, f"{autonomy.value} bypassed HIGH"


def test_a_wildcard_grant_never_covers_high_risk():
    """"Allow file tools" must not silently authorise deletion."""
    wildcard = [Grant(capability="*", granted_at=time.time())]
    assert decide(spec(risk=Risk.MEDIUM), ctx(), wildcard).outcome is Outcome.ALLOWED
    assert decide(spec(risk=Risk.HIGH), ctx(), wildcard).outcome is Outcome.NEEDS_APPROVAL


def test_pre_consent_does_not_cover_high_risk():
    """Agreeing to a scheduled task is not agreeing to whatever it later decides
    to delete. Stricter than the current implementation, which passes a blanket
    auto-confirm for scheduled work."""
    result = decide(spec(risk=Risk.HIGH), ctx(autonomy=Autonomy.PRE_CONSENTED))
    assert result.outcome is Outcome.NEEDS_APPROVAL
    assert result.escalate is True


def test_low_confidence_can_only_add_a_confirmation_never_remove_one():
    for risk in Risk:
        clear = decide(spec(risk=risk), ctx(low_confidence=False))
        unclear = decide(spec(risk=risk), ctx(low_confidence=True))
        if clear.outcome is Outcome.NEEDS_APPROVAL:
            assert unclear.outcome is Outcome.NEEDS_APPROVAL, "confidence removed a gate"


# --- ordinary behaviour ------------------------------------------------------

def test_low_risk_runs_without_asking():
    assert decide(spec(risk=Risk.LOW), ctx()).outcome is Outcome.ALLOWED


def test_low_risk_but_misheard_confirms_when_someone_is_present():
    result = decide(spec(risk=Risk.LOW), ctx(low_confidence=True))
    assert result.outcome is Outcome.NEEDS_APPROVAL


def test_low_risk_misheard_still_runs_when_nobody_is_present_to_re_ask():
    result = decide(spec(risk=Risk.LOW), ctx(low_confidence=True, autonomy=Autonomy.ESCALATE))
    assert result.outcome is Outcome.ALLOWED


def test_medium_risk_asks_interactively_but_not_under_pre_consent():
    assert decide(spec(risk=Risk.MEDIUM), ctx()).outcome is Outcome.NEEDS_APPROVAL
    assert decide(spec(risk=Risk.MEDIUM),
                  ctx(autonomy=Autonomy.PRE_CONSENTED)).outcome is Outcome.ALLOWED


def test_a_standing_grant_stops_the_repeated_asking():
    """§8: "Do not repeatedly ask for confirmation when a valid standing
    permission exists.\""""
    grants = [Grant(capability="edit_file", granted_at=time.time())]
    assert decide(spec("edit_file", Risk.MEDIUM), ctx(), grants).outcome is Outcome.ALLOWED


def test_an_expired_grant_does_not_count():
    grants = [Grant(capability="edit_file", granted_at=0, expires_at=100)]
    result = decide(spec("edit_file", Risk.MEDIUM), ctx(), grants, now=101)
    assert result.outcome is Outcome.NEEDS_APPROVAL


def test_a_grant_scoped_to_another_session_does_not_count():
    grants = [Grant(capability="edit_file", granted_at=time.time(), session_id="other")]
    result = decide(spec("edit_file", Risk.MEDIUM), ctx(session_id="s1"), grants)
    assert result.outcome is Outcome.NEEDS_APPROVAL


def test_an_exact_grant_can_cover_high_risk():
    """A wildcard cannot, but naming the capability explicitly can — that is what
    'the user has already explicitly authorized this operation' means."""
    grants = [Grant(capability="delete_file", granted_at=time.time())]
    assert decide(spec("delete_file", Risk.HIGH), ctx(), grants).outcome is Outcome.ALLOWED


# --- the allowlist ------------------------------------------------------------

def test_the_allowlist_is_enforced_here_not_by_the_caller():
    """A check living only in the turn runner would give the scheduler,
    briefings and the Live voice path no enforcement at all. A restriction
    only one caller applies is not a restriction."""
    result = decide(spec("get_time"), ctx(), allowed_names=frozenset({"read_file"}))
    assert result.outcome is Outcome.REFUSED


def test_the_allowlist_refuses_rather_than_asking():
    """Asking would not help — it is not that permission is missing, it is that
    the capability is out of scope for this task."""
    result = decide(spec("delete_file", Risk.HIGH), ctx(), allowed_names=frozenset())
    assert result.outcome is Outcome.REFUSED
    assert result.escalate is False


# --- exhaustive sweep --------------------------------------------------------

def test_no_combination_ever_lets_high_risk_through_without_an_exact_grant():
    """The floor, swept across every surface, autonomy and confidence."""
    for surface, autonomy, low_conf in itertools.product(Surface, Autonomy, (True, False)):
        result = decide(
            spec("delete_file", Risk.HIGH),
            ctx(surface=surface, autonomy=autonomy, low_confidence=low_conf),
            grants=[Grant(capability="*", granted_at=time.time())],
        )
        assert result.outcome is Outcome.NEEDS_APPROVAL, (
            f"HIGH risk slipped through: {surface.value}/{autonomy.value}/"
            f"low_confidence={low_conf}"
        )


def test_every_result_carries_a_reason_safe_to_show_the_user():
    for risk, autonomy in itertools.product(Risk, Autonomy):
        result = decide(spec(risk=risk), ctx(autonomy=autonomy))
        assert result.reason and not result.reason.endswith(("Error", "None"))


# --- "the person's own request is enough" (Phase 3) ---------------------------------

def flagged(name="act", risk=Risk.MEDIUM) -> CapabilitySpec:
    return CapabilitySpec(id=f"builtin.{name}", name=name, description="", input_schema={},
                          risk=risk, handler=lambda **_: None, request_suffices=True)


def test_a_clear_request_is_enough_for_a_medium_tool_that_declares_it():
    result = decide(flagged(), ctx())
    assert result.outcome is Outcome.ALLOWED and result.reason == "You asked for this."


def test_the_flag_is_what_makes_the_difference_and_nothing_else():
    """Without it, MEDIUM still asks interactively — the old rule, unchanged."""
    assert decide(spec(risk=Risk.MEDIUM), ctx()).outcome is Outcome.NEEDS_APPROVAL


def test_a_misheard_request_still_confirms_even_for_a_flagged_tool():
    result = decide(flagged(), ctx(low_confidence=True))
    assert result.outcome is Outcome.NEEDS_APPROVAL


def test_the_flag_never_lifts_a_high_risk_action_in_any_combination():
    """The floor, swept: surface x autonomy x confidence x wildcard grant, with the flag on."""
    for surface, autonomy, low_conf in itertools.product(Surface, Autonomy, (True, False)):
        result = decide(flagged("delete_file", Risk.HIGH),
                        ctx(surface=surface, autonomy=autonomy, low_confidence=low_conf),
                        grants=[Grant(capability="*", granted_at=time.time())])
        assert result.outcome is Outcome.NEEDS_APPROVAL, (surface, autonomy, low_conf)


def test_the_flag_changes_nothing_off_the_interactive_path():
    """Nobody here to have asked: ESCALATE still parks, and pre-consent behaves as before."""
    assert decide(flagged(), ctx(autonomy=Autonomy.ESCALATE)).outcome is Outcome.NEEDS_APPROVAL
    assert decide(flagged(), ctx(autonomy=Autonomy.ESCALATE)).escalate is True
    assert decide(flagged(), ctx(autonomy=Autonomy.PRE_CONSENTED)).outcome is Outcome.ALLOWED


def test_the_flag_does_not_widen_an_allowlist():
    assert decide(flagged("act"), ctx(), allowed_names=frozenset({"other"})).outcome \
        is Outcome.REFUSED


def test_a_flag_on_a_low_risk_spec_changes_nothing():
    assert decide(flagged(risk=Risk.LOW), ctx()).reason == "Low-risk action."
    assert decide(flagged(risk=Risk.LOW), ctx(low_confidence=True)).outcome \
        is Outcome.NEEDS_APPROVAL


#: Every MEDIUM capability the real app registers has been looked at, by name. A new one cannot
#: slip in as "asks by default" or "free by accident": it must be added to one list on purpose.
REQUEST_SUFFICES = {
    "work_in_background", "stop_working_on", "remember_about_me", "update_memory",
    "forget_something", "schedule_task", "cancel_task", "configure_briefing", "watch_for",
    "stop_watching", "undo_improvement", "start_screen_recording", "clear_notifications",
}
KEEPS_ASKING = {
    # The owner's decision: these widen what Jarvis can reach or run.
    "allow_folder", "approve_skill_scripts", "approve_skill_pipeline", "create_skill",
    # Spends money on a voice service.
    "narrate_to_file",
    # Content Management: its own rule says edits and scheduling confirm first.
    "edit_content_item", "schedule_content",
    # Jarvis's own file and browser connectors: overwriting a file or submitting a form is not
    # undoable, and no decision has been made to relax them. Open question for the owner.
    "write_file", "move_file", "browser_click", "browser_type",
}


def test_every_medium_capability_the_app_registers_has_been_decided(scratch):
    from jarvis import assembly, db

    db.reset_for_tests()
    assembly.reset_for_tests()
    try:
        registry = assembly.get_registry()
        medium = {s.name: s for s in registry.list() if s.risk is Risk.MEDIUM}
        assert set(medium) <= REQUEST_SUFFICES | KEEPS_ASKING, \
            f"undecided MEDIUM capabilities: {sorted(set(medium) - REQUEST_SUFFICES - KEEPS_ASKING)}"
        for name in REQUEST_SUFFICES & set(medium):
            assert medium[name].request_suffices, name
        for name in KEEPS_ASKING & set(medium):
            assert not medium[name].request_suffices, name
        assert REQUEST_SUFFICES <= set(medium), "a reviewed tool is no longer MEDIUM"
        # No HIGH capability may carry the flag, ever.
        assert [s.name for s in registry.list() if s.risk is Risk.HIGH and s.request_suffices] == []
    finally:
        assembly.reset_for_tests()
        db.reset_for_tests()
