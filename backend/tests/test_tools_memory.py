"""The memory tools, and the read-back the user actually answers.

The property that matters most here is the one the original enforced with a
per-tool `confirm: 'always'` flag and this build enforces through risk: a write
to memory is never silent. Testing it through the real executor rather than by
calling the handler is what proves the mechanism, not the intention.
"""

from __future__ import annotations

import pytest

from jarvis.capabilities import CapabilityRegistry, Risk
from jarvis.capabilities.execute import ExecOutcome, execute, execute_approved
from jarvis.db import reset_for_tests as reset_db
from jarvis.memory import store
from jarvis.policy import Autonomy, CallContext, Surface
from jarvis.policy import approvals as approval_store
from jarvis.tools import load_tools


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    yield
    reset_db()


@pytest.fixture
def reg():
    registry = CapabilityRegistry()
    load_tools(registry)
    return registry


def ctx(turn: str = "t1", **kw) -> CallContext:
    defaults = dict(session_id="s1", turn_id=turn, surface=Surface.TEXT,
                    autonomy=Autonomy.INTERACTIVE)
    defaults.update(kw)
    return CallContext(**defaults)


def test_saving_a_memory_is_never_silent(reg):
    result = execute("remember_about_me", {"text": "Drinks green tea."}, ctx(), registry=reg)
    assert result.outcome is ExecOutcome.NEEDS_APPROVAL
    assert store.list_memories() == [], "nothing may be written before the user answers"

    approval = approval_store.get(result.approval_id)
    assert approval.reason == 'Remember this about you: "Drinks green tea.".'


def test_the_read_back_warns_about_something_already_saved(reg):
    """So the user can choose to correct the existing note instead of ending up
    with two that say nearly the same thing."""
    store.create_memory(category="About You", text="Drinks green tea")
    result = execute("remember_about_me", {"text": "Drinks green tea."}, ctx(), registry=reg)
    assert "close to something I already have" in approval_store.get(result.approval_id).reason


def test_approving_in_a_later_turn_actually_saves_it(reg):
    result = execute("remember_about_me", {"text": "Drinks green tea."}, ctx("t1"), registry=reg)
    ran = execute_approved(result.approval_id, "t2", ctx("t2"), registry=reg)
    assert ran.ok
    saved = store.list_memories()[0]
    assert saved["text"] == "Drinks green tea."
    # 'explicit', not 'approved': the user said this directly and confirmed it.
    assert saved["origin"] == "explicit"


def test_the_same_turn_cannot_approve_its_own_request(reg):
    result = execute("remember_about_me", {"text": "Drinks green tea."}, ctx("t1"), registry=reg)
    with pytest.raises(approval_store.SameTurnRefused):
        execute_approved(result.approval_id, "t1", ctx("t1"), registry=reg)
    assert store.list_memories() == []


def test_a_correction_edits_rather_than_adding_a_second_note(reg):
    store.create_memory(category="About You", text="Uses a Mac.")
    result = execute("update_memory", {"query": "uses a mac", "new_text": "Uses a Windows PC."},
                     ctx("t1"), registry=reg)
    assert approval_store.get(result.approval_id).reason == \
        'Change "Uses a Mac." to "Uses a Windows PC."?'

    execute_approved(result.approval_id, "t2", ctx("t2"), registry=reg)
    remaining = store.list_memories()
    assert len(remaining) == 1 and remaining[0]["text"] == "Uses a Windows PC."


def test_a_correction_with_nothing_to_correct_says_so_in_the_read_back(reg):
    result = execute("update_memory", {"query": "something I never said", "new_text": "x"},
                     ctx(), registry=reg)
    assert "couldn't find anything" in approval_store.get(result.approval_id).reason


def test_forgetting_archives_rather_than_destroying(reg):
    memory = store.create_memory(category="About You", text="Uses a Mac.")
    result = execute("forget_something", {"query": "uses a mac"}, ctx("t1"), registry=reg)
    ran = execute_approved(result.approval_id, "t2", ctx("t2"), registry=reg)

    assert ran.ok and store.list_memories() == []
    assert store.get_memory(memory["id"])["archived"] is True


def test_reading_what_is_pending_needs_no_approval(reg):
    store.create_candidate(source_kind="chat", category="About You",
                           text="Might live in Lagos.", confidence=0.3)
    result = execute("review_memories", {}, ctx(), registry=reg)
    assert result.ok and result.value["pending"][0]["text"] == "Might live in Lagos."


def test_every_memory_write_is_classified_medium_and_every_read_low(reg):
    for name in ("remember_about_me", "update_memory", "forget_something"):
        assert reg.get(name).risk is Risk.MEDIUM, f"{name} must not be silent"
    for name in ("review_memories", "checkpoint_memories"):
        assert reg.get(name).risk is Risk.LOW


def test_a_broken_summary_does_not_block_the_gate(reg):
    """A capability that cannot describe itself still gets confirmed — with the
    policy's own wording — rather than failing open or failing shut."""
    spec = reg.get("remember_about_me")
    reg.unregister(spec.name)
    from dataclasses import replace

    def explode(_args):
        raise RuntimeError("bad summary")

    reg.register(replace(spec, summarize=explode))
    result = execute("remember_about_me", {"text": "x"}, ctx(), registry=reg)
    assert result.outcome is ExecOutcome.NEEDS_APPROVAL
    assert "needs confirming" in approval_store.get(result.approval_id).reason
