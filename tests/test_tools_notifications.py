"""The notification tools — read-only status and marking-read run immediately;
clearing moves things to the recycle bin at once (the request suffices) and emptying it is confirmed, through the
real executor, the same way the memory tools are (`test_tools_memory.py`).
"""

from __future__ import annotations

import pytest

from jarvis import notifications
from jarvis.capabilities import CapabilityRegistry
from jarvis.capabilities.execute import ExecOutcome, execute, execute_approved
from jarvis.policy import Autonomy, CallContext, Surface
from jarvis.policy import approvals as approval_store
from jarvis.tools import load_tools


@pytest.fixture(autouse=True)
def _isolate(scratch):
    yield


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


def test_status_answers_how_many_are_read_and_unread(reg):
    notifications.add(kind="system", title="one")
    two = notifications.add(kind="system", title="two")
    notifications.mark_read(two["id"])

    result = execute("notifications_status", {}, ctx(), registry=reg)
    assert result.outcome is ExecOutcome.COMPLETED
    assert result.value == {"ok": True, "total": 2, "unread": 1, "read": 1, "inRecycleBin": 0}


def test_mark_all_read_runs_immediately_no_confirmation_needed(reg):
    notifications.add(kind="system", title="one")
    notifications.add(kind="system", title="two")

    result = execute("mark_notifications_read", {}, ctx(), registry=reg)
    assert result.outcome is ExecOutcome.COMPLETED
    assert notifications.unread_count() == 0


def test_clear_runs_at_once_on_a_plain_request_and_only_moves_to_the_recycle_bin(reg):
    notifications.add(kind="system", title="one")
    notifications.add(kind="system", title="two")

    # MEDIUM and undoable (`request_suffices`): the person's own request is the go-ahead.
    result = execute("clear_notifications", {}, ctx("t1"), registry=reg)
    assert result.ok and result.outcome is not ExecOutcome.NEEDS_APPROVAL
    assert notifications.listed() == []
    assert len(notifications.trash_listed()) == 2, "moved to the recycle bin, not deleted"


def test_clear_still_asks_when_nobody_is_here_to_have_asked(reg):
    notifications.add(kind="system", title="one")

    result = execute("clear_notifications", {}, ctx("t1", autonomy=Autonomy.ESCALATE), registry=reg)
    assert result.outcome is ExecOutcome.NEEDS_APPROVAL
    assert notifications.listed() != []


def test_emptying_the_recycle_bin_is_high_risk_and_irreversible(reg):
    notifications.add(kind="system", title="one")
    notifications.clear_all()
    assert len(notifications.trash_listed()) == 1

    result = execute("empty_notifications_recycle_bin", {}, ctx("t1"), registry=reg)
    assert result.outcome is ExecOutcome.NEEDS_APPROVAL
    assert len(notifications.trash_listed()) == 1, "nothing may be purged before confirming"

    ran = execute_approved(result.approval_id, "t2", ctx("t2"), registry=reg)
    assert ran.ok
    assert notifications.trash_listed() == []


def test_the_same_turn_cannot_approve_its_own_empty_bin_request(reg):
    notifications.add(kind="system", title="one")
    notifications.clear_all()

    result = execute("empty_notifications_recycle_bin", {}, ctx("t1"), registry=reg)
    with pytest.raises(approval_store.SameTurnRefused):
        execute_approved(result.approval_id, "t1", ctx("t1"), registry=reg)
    assert len(notifications.trash_listed()) == 1
