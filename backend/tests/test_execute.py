"""The capability executor (§5, §45, §47, §49).

§47 says to identify failure modes before implementing, so the tests are mostly
failure modes: timeout, invalid arguments, a handler that raises, a duplicate
delivery, and a refusal. The honesty requirement (§45) gets its own test — the
executor must never claim to have stopped something it merely stopped waiting for.
"""

from __future__ import annotations

import threading
import time

import pytest

from jarvis.capabilities import CapabilityRegistry, CapabilitySpec, RetryPolicy, Risk
from jarvis.capabilities.execute import ExecOutcome, execute, execute_approved
from jarvis.db import reset_for_tests as reset_db
from jarvis.events import EventType
from jarvis.events.bus import EventBus
from jarvis.policy import Autonomy, CallContext, Surface
from jarvis.policy import approvals as ap


@pytest.fixture(autouse=True)
def _db(scratch):
    reset_db()
    yield
    reset_db()


@pytest.fixture
def reg():
    return CapabilityRegistry()


def ctx(**kw) -> CallContext:
    defaults = dict(session_id="s1", turn_id="t1", surface=Surface.TEXT,
                    autonomy=Autonomy.INTERACTIVE)
    defaults.update(kw)
    return CallContext(**defaults)


def add(reg, name="act", risk=Risk.LOW, handler=None, **kw) -> CapabilitySpec:
    return reg.register(CapabilitySpec(
        id=f"builtin.{name}", name=name, description="", risk=risk,
        input_schema=kw.pop("input_schema", {"type": "object", "properties": {}}),
        handler=handler or (lambda **_: "done"), **kw,
    ))


# --- the happy path ----------------------------------------------------------

def test_a_low_risk_capability_just_runs(reg):
    add(reg, "get_time", handler=lambda **_: "12:00")
    result = execute("get_time", {}, ctx(), registry=reg)
    assert result.ok and result.outcome is ExecOutcome.COMPLETED
    assert result.value == "12:00"
    assert result.attempts == 1


def test_an_unknown_capability_is_refused_not_crashed(reg):
    result = execute("nonexistent", {}, ctx(), registry=reg)
    assert result.outcome is ExecOutcome.REFUSED
    assert "don't have a capability" in result.error


# --- §47 failure modes -------------------------------------------------------

def test_invalid_arguments_are_refused_clearly(reg):
    """§47: "What happens if the model returns invalid tool arguments?" A clear
    refusal, not a TypeError from inside the handler."""
    add(reg, "search", input_schema={
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    })
    missing = execute("search", {}, ctx(), registry=reg)
    assert missing.outcome is ExecOutcome.INVALID_ARGUMENTS
    assert "query" in missing.error

    wrong = execute("search", {"query": 42}, ctx(operation_id="op2"), registry=reg)
    assert wrong.outcome is ExecOutcome.INVALID_ARGUMENTS


def test_a_handler_that_raises_becomes_an_honest_failure(reg):
    def boom(**_):
        raise RuntimeError("disk on fire")

    add(reg, "risky_read", handler=boom)
    result = execute("risky_read", {}, ctx(), registry=reg)
    assert result.ok is False
    assert result.outcome is ExecOutcome.FAILED
    assert "disk on fire" in result.error


def test_a_timeout_never_claims_the_work_was_stopped(reg):
    """§45. Python cannot kill a thread, so a non-cancellable capability that
    overruns has only stopped being WAITED for. Saying otherwise is the fake
    behaviour the directive forbids."""
    add(reg, "slow", handler=lambda **_: time.sleep(5), timeout_s=0.1)
    result = execute("slow", {}, ctx(), registry=reg)
    assert result.outcome is ExecOutcome.TIMED_OUT
    assert result.outcome is not ExecOutcome.CANCELLED
    assert result.work_may_continue is True
    assert "may still be running" in result.error


def test_a_cancellable_capability_is_genuinely_asked_to_stop(reg):
    stopped = threading.Event()

    def waits(cancel, **_):
        for _ in range(100):
            if cancel.wait(0.02):
                stopped.set()
                return "stopped"
        return "finished"

    add(reg, "watchable", handler=waits, cancellable=True, timeout_s=0.1)
    result = execute("watchable", {}, ctx(), registry=reg)
    assert result.outcome is ExecOutcome.CANCELLED
    assert result.work_may_continue is False
    assert stopped.wait(2), "the handler was never actually signalled"


def test_retries_happen_and_are_counted(reg):
    calls = {"n": 0}

    def flaky(**_):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient")
        return "ok at last"

    add(reg, "flaky", handler=flaky, retry=RetryPolicy(attempts=2, backoff_s=0))
    result = execute("flaky", {}, ctx(), registry=reg)
    assert result.ok and result.value == "ok at last"
    assert result.attempts == 3


def test_retries_are_exhausted_honestly(reg):
    def always(**_):
        raise RuntimeError("still broken")

    add(reg, "broken", handler=always, retry=RetryPolicy(attempts=1, backoff_s=0))
    result = execute("broken", {}, ctx(), registry=reg)
    assert result.ok is False
    assert result.attempts == 2


# --- §49 idempotency ---------------------------------------------------------

def test_the_same_operation_delivered_twice_runs_once(reg):
    """§49's own example: "Create reminder for 9 AM" must not create two."""
    calls = {"n": 0}

    def create(**_):
        calls["n"] += 1
        return {"reminder": "9am"}

    add(reg, "create_reminder", handler=create)
    c = ctx(operation_id="op-reminder")

    first = execute("create_reminder", {}, c, registry=reg)
    second = execute("create_reminder", {}, c, registry=reg)

    assert calls["n"] == 1, "the side effect ran twice"
    assert first.value == second.value
    assert second.outcome is ExecOutcome.DUPLICATE


def test_idempotency_survives_a_restart(reg):
    calls = {"n": 0}
    add(reg, "create_reminder", handler=lambda **_: calls.__setitem__("n", calls["n"] + 1))
    c = ctx(operation_id="op-restart")

    execute("create_reminder", {}, c, registry=reg)
    reset_db()                       # fresh process
    execute("create_reminder", {}, c, registry=reg)
    assert calls["n"] == 1


def test_different_operations_both_run(reg):
    calls = {"n": 0}
    add(reg, "act", handler=lambda **_: calls.__setitem__("n", calls["n"] + 1))
    execute("act", {}, ctx(operation_id="a"), registry=reg)
    execute("act", {}, ctx(operation_id="b"), registry=reg)
    assert calls["n"] == 2


# --- policy integration ------------------------------------------------------

def test_a_high_risk_capability_does_not_run_it_asks(reg):
    ran = {"yes": False}
    add(reg, "delete_file", risk=Risk.HIGH,
        handler=lambda **_: ran.__setitem__("yes", True))

    result = execute("delete_file", {"path": "/x"}, ctx(), registry=reg)
    assert result.outcome is ExecOutcome.NEEDS_APPROVAL
    assert ran["yes"] is False, "a high-risk action ran before being approved"
    assert result.approval_id is not None


def test_approving_in_a_later_turn_actually_runs_it(reg):
    ran = {"args": None}
    add(reg, "delete_file", risk=Risk.HIGH,
        handler=lambda **kw: ran.__setitem__("args", kw) or "deleted")

    asked = execute("delete_file", {"path": "/x"}, ctx(turn_id="t1"), registry=reg)
    result = execute_approved(asked.approval_id, resolving_turn_id="t2",
                              ctx=ctx(turn_id="t2"), registry=reg)
    assert result.ok and result.value == "deleted"
    assert ran["args"] == {"path": "/x"}, "executed with different arguments than described"


def test_approving_within_the_same_turn_is_refused(reg):
    add(reg, "delete_file", risk=Risk.HIGH, handler=lambda **_: "deleted")
    asked = execute("delete_file", {"path": "/x"}, ctx(turn_id="t1"), registry=reg)
    with pytest.raises(ap.SameTurnRefused):
        execute_approved(asked.approval_id, resolving_turn_id="t1",
                         ctx=ctx(turn_id="t1"), registry=reg)


def test_the_allowlist_is_enforced_by_the_executor(reg):
    ran = {"yes": False}
    add(reg, "get_time", handler=lambda **_: ran.__setitem__("yes", True))
    result = execute("get_time", {}, ctx(), registry=reg,
                     allowed_names=frozenset({"read_file"}))
    assert result.outcome is ExecOutcome.REFUSED
    assert ran["yes"] is False


# --- §38 events --------------------------------------------------------------

def test_a_successful_run_publishes_started_then_completed(reg):
    eb = EventBus()
    seen = []
    eb.subscribe(None, seen.append)
    add(reg, "act")
    execute("act", {}, ctx(), registry=reg, event_bus=eb)
    assert [e.type for e in seen] == [EventType.TOOL_STARTED, EventType.TOOL_COMPLETED]


def test_a_failure_publishes_started_then_failed(reg):
    eb = EventBus()
    seen = []
    eb.subscribe(None, seen.append)

    def boom(**_):
        raise RuntimeError("nope")

    add(reg, "act", handler=boom)
    execute("act", {}, ctx(), registry=reg, event_bus=eb)
    assert [e.type for e in seen] == [EventType.TOOL_STARTED, EventType.TOOL_FAILED]


def test_published_arguments_are_redacted(reg):
    """§25: never log secrets."""
    eb = EventBus()
    seen = []
    eb.subscribe(EventType.TOOL_STARTED, seen.append)
    add(reg, "connect", redact_args=frozenset({"api_key"}))
    execute("connect", {"api_key": "sk-secret", "name": "svc"}, ctx(),
            registry=reg, event_bus=eb)
    assert seen[0].payload["args"]["api_key"] == "<redacted>"
    assert "sk-secret" not in str(seen[0].payload)
