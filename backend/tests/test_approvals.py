"""Persisted approvals and standing grants (§8, §40).

The behaviours tested are the ones the in-memory token map cannot provide:
survival across a restart, a real timeout sweep, idempotency, and a same-turn
refusal that cannot be skipped by a caller who omitted its context.
"""

from __future__ import annotations

import pytest

from jarvis.capabilities import CapabilitySpec, Risk
from jarvis.db import reset_for_tests as reset_db
from jarvis.events import EventType
from jarvis.events.bus import EventBus
from jarvis.jscompat import now_iso
from jarvis.policy import Autonomy, CallContext, Outcome, Surface, decide
from jarvis.policy import approvals as ap


def spec(name="delete_file", risk=Risk.HIGH, **kw) -> CapabilitySpec:
    return CapabilitySpec(
        id=f"builtin.{name}", name=name, description="", input_schema={},
        risk=risk, handler=lambda **_: None, **kw,
    )


def ctx(**kw) -> CallContext:
    defaults = dict(session_id="s1", turn_id="t1", surface=Surface.TEXT,
                    autonomy=Autonomy.INTERACTIVE)
    defaults.update(kw)
    return CallContext(**defaults)


@pytest.fixture(autouse=True)
def _db(scratch):
    reset_db()
    yield
    reset_db()


def test_requesting_records_a_pending_approval():
    a = ap.request(spec(), {"path": "/tmp/x"}, ctx(), "needs confirming")
    assert a.is_pending
    assert a.capability == "delete_file"
    assert ap.get(a.id).is_pending


def test_a_pending_approval_survives_a_restart():
    """The whole point of persisting these. TEST 12 expects state to survive."""
    a = ap.request(spec(), {}, ctx(), "needs confirming")
    reset_db()                      # simulates a fresh process
    still_there = ap.pending("s1")
    assert [x.id for x in still_there] == [a.id]


def test_resolving_within_the_same_turn_is_refused():
    """The model minting a question and answering it in the same breath —
    reproduced live on the voice path, where the protection cannot fire today."""
    a = ap.request(spec(), {}, ctx(turn_id="t1"), "needs confirming")
    with pytest.raises(ap.SameTurnRefused):
        ap.resolve(a.id, ap.Resolution.ALLOW, resolving_turn_id="t1")
    assert ap.get(a.id).is_pending   # and it stays pending


def test_resolving_in_a_later_turn_works():
    a = ap.request(spec(), {}, ctx(turn_id="t1"), "needs confirming")
    resolved = ap.resolve(a.id, ap.Resolution.ALLOW, resolving_turn_id="t2")
    assert resolved.status is ap.Resolution.ALLOW
    assert ap.pending("s1") == []


def test_all_four_resolutions_are_supported():
    """§8 names ALLOW, DENY, CANCEL and TIMEOUT."""
    for resolution in (ap.Resolution.ALLOW, ap.Resolution.DENY, ap.Resolution.CANCEL,
                       ap.Resolution.TIMEOUT):
        a = ap.request(spec(), {}, ctx(operation_id=f"op-{resolution.value}"), "r")
        out = ap.resolve(a.id, resolution, resolving_turn_id="later")
        assert out.status is resolution


def test_a_second_answer_cannot_flip_the_first():
    """A stray second click must not turn a denial into an approval."""
    a = ap.request(spec(), {}, ctx(), "r")
    ap.resolve(a.id, ap.Resolution.DENY, resolving_turn_id="t2")
    again = ap.resolve(a.id, ap.Resolution.ALLOW, resolving_turn_id="t3")
    assert again.status is ap.Resolution.DENY


def test_the_same_operation_asked_twice_asks_once():
    """§49: a request delivered twice must not produce two questions."""
    c = ctx(operation_id="op-42")
    first = ap.request(spec(), {}, c, "r")
    second = ap.request(spec(), {}, c, "r")
    assert first.id == second.id
    assert len(ap.pending("s1")) == 1


def test_secrets_are_redacted_before_being_stored():
    """§25: never log secrets, and which arguments are secret is the
    capability's declaration, not each call site's problem."""
    s = spec("connect_service", risk=Risk.MEDIUM, redact_args=frozenset({"api_key"}))
    a = ap.request(s, {"api_key": "sk-realsecret", "name": "svc"}, ctx(), "r")
    assert a.args["api_key"] == "<redacted>"
    assert a.args["name"] == "svc"
    assert "sk-realsecret" not in str(ap.get(a.id).args)


def test_stale_approvals_time_out_rather_than_lingering():
    """A real sweep, not a TTL noticed only if someone happens to redeem."""
    ap.request(spec(), {}, ctx(), "r")
    assert len(ap.pending()) == 1
    swept = ap.expire_older_than("2999-01-01T00:00:00.000Z")
    assert swept == 1
    assert ap.pending() == []


def test_requesting_and_resolving_publish_events():
    eb = EventBus()
    seen = []
    eb.subscribe(EventType.APPROVAL_REQUESTED, seen.append)
    eb.subscribe(EventType.APPROVAL_RESOLVED, seen.append)

    a = ap.request(spec(), {}, ctx(), "r", event_bus=eb)
    ap.resolve(a.id, ap.Resolution.ALLOW, resolving_turn_id="t2", event_bus=eb)

    assert [e.type for e in seen] == [EventType.APPROVAL_REQUESTED, EventType.APPROVAL_RESOLVED]
    assert seen[0].payload["risk"] == "high"


# --- standing grants ---------------------------------------------------------

def test_a_granted_capability_stops_being_asked_about():
    s = spec("edit_file", risk=Risk.MEDIUM)
    assert decide(s, ctx(), ap.active_grants("s1")).outcome is Outcome.NEEDS_APPROVAL
    ap.grant("edit_file")
    assert decide(s, ctx(), ap.active_grants("s1")).outcome is Outcome.ALLOWED


def test_a_revoked_grant_stops_counting():
    gid = ap.grant("edit_file")
    ap.revoke(gid)
    assert ap.active_grants("s1") == []


def test_a_grant_scoped_to_one_session_does_not_leak_to_another():
    ap.grant("edit_file", session_id="s1")
    assert len(ap.active_grants("s1")) == 1
    assert ap.active_grants("s2") == []


def test_a_wildcard_grant_still_cannot_cover_high_risk():
    """The floor lives in the policy, so persistence cannot weaken it."""
    ap.grant("*")
    grants = ap.active_grants("s1")
    assert decide(spec("edit_file", Risk.MEDIUM), ctx(), grants).outcome is Outcome.ALLOWED
    assert decide(spec("delete_file", Risk.HIGH), ctx(), grants).outcome is Outcome.NEEDS_APPROVAL
