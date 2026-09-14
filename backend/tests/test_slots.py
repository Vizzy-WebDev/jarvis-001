"""Role slots: which model does which job.

The test this file exists for is `test_a_benched_assignment_still_leaves_a_turn
_answerable`. The obvious implementation of "voice uses this model" is a filter,
and a filter means a voice turn FAILS when that one deployment is rate-limited —
trading an occasional slightly-worse answer for an occasional no answer at all.

So an assignment resolves to a PIN, and the router already honours a pin by
moving it to the front rather than by removing everything else. That is checked
here against the real ranking function rather than asserted about it, because
the working case passes under either design and only the benched one tells them
apart.
"""

from __future__ import annotations

import pytest

from jarvis.catalog import Effort
from jarvis.gateway import availability, routing, slots
from jarvis.gateway.slots import Role


@pytest.fixture(autouse=True)
def _isolated(scratch):
    slots.reset_for_tests()
    availability.reset_for_tests()
    yield
    slots.reset_for_tests()
    availability.reset_for_tests()


def _entry(entry_id: str, *, speed: int = 3, quality: int = 3) -> dict:
    """A candidate shaped the way the ranking function reads them."""
    return {
        "id": entry_id, "model": entry_id, "enabled": True, "keyRequired": False,
        "caps": {"tools": True}, "tier": {"speed": speed, "quality": quality, "cost": 1},
    }


ROSTER = [_entry("quick", speed=5, quality=2), _entry("careful", speed=1, quality=5)]


# --- the shape of an assignment --------------------------------------------

def test_every_role_starts_unassigned_and_that_is_a_real_answer():
    """A fresh install with one model needs no configuration at all."""
    for role in Role:
        slot = slots.get(role)
        assert slot.deployment_id is None
        assert slot.effort is None
        assert slot.assigned is False


def test_a_role_can_be_given_a_model():
    slots.assign(Role.VOICE, deployment_id="quick")

    assert slots.get(Role.VOICE).deployment_id == "quick"
    assert slots.pin_for(Role.VOICE) == "quick"
    assert slots.pin_for(Role.CONVERSATION) is None, "roles are independent"


def test_a_role_can_be_given_an_effort_without_a_model():
    """The more useful half for anyone without a favourite model: "whatever
    gets picked, ask it to think this hard"."""
    slots.assign(Role.CONTROL, effort=Effort.HIGH)

    slot = slots.get(Role.CONTROL)
    assert slot.effort is Effort.HIGH
    assert slot.deployment_id is None
    assert slot.assigned is True


def test_a_role_can_be_given_a_model_without_an_effort():
    slots.assign(Role.BACKGROUND, deployment_id="quick")

    assert slots.effort_for(Role.BACKGROUND) is None, "the version's own default stands"


def test_the_two_halves_are_set_independently():
    slots.assign(Role.VOICE, deployment_id="quick")
    slots.assign(Role.VOICE, effort=Effort.MINIMAL)

    slot = slots.get(Role.VOICE)
    assert slot.deployment_id == "quick"
    assert slot.effort is Effort.MINIMAL


def test_clearing_a_role_hands_it_back_to_ranking():
    slots.assign(Role.VOICE, deployment_id="quick", effort=Effort.MINIMAL)
    slots.clear(Role.VOICE)

    assert slots.get(Role.VOICE).assigned is False


def test_an_assignment_survives_a_restart():
    slots.assign(Role.VOICE, deployment_id="quick", effort=Effort.MAX)
    slots.reset_for_tests()

    slot = slots.get(Role.VOICE)
    assert slot.deployment_id == "quick"
    assert slot.effort is Effort.MAX


# --- leads, never restricts -------------------------------------------------

def test_an_assignment_moves_its_model_to_the_front():
    """What a slot is for: the ranking would pick the other one."""
    task = routing.Task(text="hello")
    unpinned = routing.build_candidates(task, entries=ROSTER)
    assert unpinned[0]["id"] == "quick", "ranking alone prefers the fast one"

    slots.assign(Role.CONVERSATION, deployment_id="careful")
    pinned = routing.build_candidates(
        task, entries=ROSTER, manual_model_id=slots.pin_for(Role.CONVERSATION))

    assert pinned[0]["id"] == "careful"


def test_an_assignment_does_not_remove_the_others():
    """The whole roster stays available behind the pin, which is what makes a
    mid-turn failure recoverable rather than fatal."""
    slots.assign(Role.CONVERSATION, deployment_id="careful")

    ranked = routing.build_candidates(
        routing.Task(text="hello"), entries=ROSTER,
        manual_model_id=slots.pin_for(Role.CONVERSATION))

    assert [e["id"] for e in ranked] == ["careful", "quick"]


def test_a_benched_assignment_still_leaves_a_turn_answerable():
    """The criterion this phase is measured by.

    A slot pointing at a rate-limited deployment must not take the role down
    with it. Under a filtering implementation this list comes back empty and the
    turn fails; under a pin it comes back with the other candidate.
    """
    slots.assign(Role.VOICE, deployment_id="careful")
    availability.record("careful", "quota", detail="out of credit")

    ranked = routing.build_candidates(
        routing.Task(text="say something"), entries=ROSTER,
        manual_model_id=slots.pin_for(Role.VOICE))

    assert [e["id"] for e in ranked] == ["quick"], "the turn is still answerable"


def test_an_assignment_to_something_that_no_longer_exists_degrades_quietly():
    """A stale preference is not an error. The id may name a deployment the
    user deleted, or an old model-row id adopted from a dormant preference."""
    slots.assign(Role.CONVERSATION, deployment_id="deleted-long-ago")

    ranked = routing.build_candidates(
        routing.Task(text="hello"), entries=ROSTER,
        manual_model_id=slots.pin_for(Role.CONVERSATION))

    assert [e["id"] for e in ranked] == ["quick", "careful"], "ordinary ranking"


def test_an_assignment_to_a_switched_off_model_degrades_too():
    roster = [_entry("quick", speed=5, quality=2), {**_entry("careful"), "enabled": False}]
    slots.assign(Role.CONVERSATION, deployment_id="careful")

    ranked = routing.build_candidates(
        routing.Task(text="hello"), entries=roster,
        manual_model_id=slots.pin_for(Role.CONVERSATION))

    assert [e["id"] for e in ranked] == ["quick"]


# --- keeping the store honest ----------------------------------------------

def test_a_deleted_deployment_stops_being_named_by_any_role():
    """A dangling assignment already degrades, but a screen listing what each
    role is set to should not go on naming something the user removed — and a
    freed id would otherwise hand the next deployment a role it never got."""
    slots.assign(Role.VOICE, deployment_id="doomed")
    slots.assign(Role.BACKGROUND, deployment_id="doomed", effort=Effort.LOW)
    slots.assign(Role.CONTROL, deployment_id="kept")

    removed = slots.forget_deployment("doomed")

    assert removed == 2
    assert slots.get(Role.VOICE).assigned is False
    assert slots.get(Role.BACKGROUND).deployment_id is None
    assert slots.get(Role.BACKGROUND).effort is Effort.LOW, "the effort half survives"
    assert slots.get(Role.CONTROL).deployment_id == "kept"


def test_all_five_roles_are_reported_whether_set_or_not():
    """A screen has to be able to show the unassigned ones — they are the ones
    a person most needs to see in order to set them."""
    slots.assign(Role.VOICE, deployment_id="quick")
    everything = slots.all_slots()

    assert set(everything) == set(Role)
    assert everything[Role.VOICE].deployment_id == "quick"
    assert everything[Role.CONTROL].assigned is False


# --- the preferences that were never read ----------------------------------

def test_a_pin_that_silently_did_nothing_is_adopted_rather_than_dropped():
    """`manualModelId` and `voiceModelId` were stored, served, and consulted by
    nothing. Anyone who set one had been running with a control that did not
    work; adopting the value honours an intent that was dropped."""
    from jarvis import prefs

    prefs.set_prefs({"manualModelId": "chosen-by-hand", "voiceModelId": "chosen-for-voice"})
    slots.reset_for_tests()

    assert slots.pin_for(Role.CONVERSATION) == "chosen-by-hand"
    assert slots.pin_for(Role.VOICE) == "chosen-for-voice"


def test_the_adopted_keys_are_swept_out_of_the_preferences_file():
    """`set_prefs` merges, so it can change a value but never drop one: a key
    written by an older build survives every later write and goes on being
    served to a screen that might still offer it."""
    from jarvis import prefs

    prefs.set_prefs({"manualModelId": "x", "voiceModelId": "y", "autoSelect": False})
    slots.reset_for_tests()
    slots.get(Role.VOICE)  # triggers the one-time adoption

    after = prefs.get_prefs()
    for gone in ("manualModelId", "voiceModelId", "autoSelect"):
        assert gone not in after, f"{gone} should no longer be served"


def test_autoselect_is_deleted_rather_than_moved():
    """It expressed "use the manual pick instead of ranking", which a pin either
    existing or not already says. A separate boolean for it could only ever
    disagree with the thing it described."""
    from jarvis import prefs

    assert "autoSelect" not in prefs.DEFAULTS
    assert "autoSelect" not in prefs.get_prefs()


def test_adoption_happens_once_and_does_not_fight_a_later_change():
    """Once the slot store exists it is the answer; the preferences file is not
    consulted again, so clearing a role stays cleared."""
    from jarvis import prefs

    prefs.set_prefs({"voiceModelId": "from-prefs"})
    slots.reset_for_tests()
    assert slots.pin_for(Role.VOICE) == "from-prefs"

    slots.clear(Role.VOICE)
    slots.reset_for_tests()

    assert slots.pin_for(Role.VOICE) is None


def test_nothing_is_written_when_there_is_nothing_to_adopt(scratch):
    """A fresh install should not gain a file it has no use for."""
    slots.reset_for_tests()
    slots.get(Role.VOICE)

    assert not (scratch.data_dir / "model-slots.json").exists()


def test_filtering_instead_of_pinning_would_leave_the_turn_unanswerable():
    """The alternative, demonstrated rather than described.

    Same roster, same benched assignment, both designs side by side. A filter
    is the shorter implementation and the one that reads most directly as "voice
    uses this model" — and it trades an occasional slightly-worse answer for an
    occasional no answer at all.
    """
    slots.assign(Role.VOICE, deployment_id="careful")
    availability.record("careful", "quota", detail="out of credit")
    task = routing.Task(text="say something")
    pin = slots.pin_for(Role.VOICE)

    # The tempting implementation: restrict the roster to the assigned model.
    filtered = routing.build_candidates(
        task, entries=[e for e in ROSTER if e["id"] == pin])
    assert filtered == [], "this is the bug: a rate-limited assignment kills the turn"

    # What this build does: lead the ranking, never restrict it.
    ranked = routing.build_candidates(task, entries=ROSTER, manual_model_id=pin)
    assert [e["id"] for e in ranked] == ["quick"]
