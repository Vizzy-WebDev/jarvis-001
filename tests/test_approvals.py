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


def test_secrets_are_redacted_on_the_way_out_not_on_the_way_in():
    """§25: never log secrets. Redaction applies to what is DISPLAYED.

    The stored row keeps the real arguments because approving must execute
    exactly what was described — capturing them at request time is what stops the
    model altering an argument between asking and running. An earlier version of
    this redacted before storing, which meant approving a capability with a
    secret argument would have executed it with the literal string "<redacted>".
    """
    s = spec("connect_service", risk=Risk.MEDIUM, redact_args=frozenset({"api_key"}))
    a = ap.request(s, {"api_key": "sk-realsecret", "name": "svc"}, ctx(), "r")

    assert a.safe_args["api_key"] == "<redacted>"      # what may be shown
    assert a.safe_args["name"] == "svc"
    assert a.args["api_key"] == "sk-realsecret"        # what will be executed

    reloaded = ap.get(a.id)
    assert reloaded.safe_args["api_key"] == "<redacted>"
    assert reloaded.args["api_key"] == "sk-realsecret"


def test_a_published_approval_event_never_carries_the_secret():
    eb = EventBus()
    seen = []
    eb.subscribe(EventType.APPROVAL_REQUESTED, seen.append)
    s = spec("connect_service", risk=Risk.MEDIUM, redact_args=frozenset({"api_key"}))
    ap.request(s, {"api_key": "sk-realsecret"}, ctx(), "r", event_bus=eb)
    assert "sk-realsecret" not in str(seen[0].payload)
    assert seen[0].payload["args"]["api_key"] == "<redacted>"


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


# --- expiry: an old "yes" is never a go-ahead ---------------------------------

def _age(approval_id: str, seconds: float) -> None:
    """Make a question look `seconds` older than it is (both the ask time and the deadline)."""
    from datetime import datetime, timedelta, timezone

    from jarvis.db import get_db

    def shifted(iso):
        moment = datetime.fromisoformat(iso.replace("Z", "+00:00")) - timedelta(seconds=seconds)
        return moment.astimezone(timezone.utc).isoformat()

    row = get_db().execute("SELECT requested_at, expires_at FROM approvals WHERE id = ?",
                           (approval_id,)).fetchone()
    get_db().execute("UPDATE approvals SET requested_at = ?, expires_at = ? WHERE id = ?",
                     (shifted(row["requested_at"]),
                      shifted(row["expires_at"]) if row["expires_at"] else None, approval_id))


def test_a_question_in_a_chat_gets_a_deadline_and_a_job_does_not():
    chat = ap.request(spec(), {}, ctx(operation_id="o1"), "r")
    job = ap.request(spec(), {}, ctx(session_id="job:j1", autonomy=Autonomy.ESCALATE,
                                     operation_id="o2"), "r")
    assert chat.expires_at is not None
    assert job.expires_at is None


def test_an_answer_that_comes_too_late_is_refused_and_closes_the_question():
    a = ap.request(spec(), {}, ctx(), "r")
    _age(a.id, ap.APPROVAL_TTL_S + 5)
    late = ap.resolve(a.id, ap.Resolution.ALLOW, resolving_turn_id="t2")
    assert late.status is ap.Resolution.TIMEOUT
    assert late.resolved_by == "system"
    assert ap.get(a.id).status is ap.Resolution.TIMEOUT


def test_an_expired_approval_never_executes():
    from jarvis.capabilities import CapabilityRegistry
    from jarvis.capabilities.execute import execute_approved

    ran: list[int] = []
    s = spec(name="delete_file")
    s = CapabilitySpec(id=s.id, name=s.name, description="", input_schema={}, risk=Risk.HIGH,
                       handler=lambda **_: ran.append(1) or {"ok": True})
    registry = CapabilityRegistry()
    registry.register(s)
    a = ap.request(s, {}, ctx(), "r")
    _age(a.id, ap.APPROVAL_TTL_S + 5)
    result = execute_approved(a.id, "t2", ctx(turn_id="t2", operation_id=a.operation_id),
                              registry=registry)
    assert not result.ok
    assert "timed out" in result.error
    assert ran == []


def test_the_listing_never_offers_an_expired_question():
    a = ap.request(spec(), {}, ctx(), "r")
    _age(a.id, ap.APPROVAL_TTL_S + 5)
    assert ap.pending() == []
    assert ap.get(a.id).status is ap.Resolution.TIMEOUT


def test_a_row_from_before_expiry_existed_counts_by_its_age():
    """The backlog: pending rows with no deadline written, asked long ago in a chat."""
    from jarvis.db import get_db

    old = ap.request(spec(), {}, ctx(operation_id="o-old"), "r")
    jobs = ap.request(spec(), {}, ctx(session_id="job:j1", autonomy=Autonomy.ESCALATE,
                                      operation_id="o-job"), "r")
    get_db().execute("UPDATE approvals SET expires_at = NULL, requested_at = '2026-09-21T10:00:00.000Z'")
    assert ap.expire_stale() == 1
    assert ap.get(old.id).status is ap.Resolution.TIMEOUT
    assert ap.get(jobs.id).is_pending          # background work waits as long as it must


def test_a_fresh_question_can_still_be_answered():
    a = ap.request(spec(), {}, ctx(), "r")
    assert ap.resolve(a.id, ap.Resolution.ALLOW, resolving_turn_id="t2").status is ap.Resolution.ALLOW
