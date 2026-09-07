"""The scheduler: recurrence maths, the tick loop, and the tools.

Recurrence is pure, so it is tested as a table against a fixed clock — including
the two cases that are easy to get subtly wrong and impossible to notice: a
weekday schedule landing on a weekend, and a daily schedule crossing a
daylight-saving boundary.

The engine is tested through `tick()` with an injected clock rather than by
waiting, and the interlock that keeps the timer off is tested directly, because
the failure it prevents — two schedulers firing the same task — is silent.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from jarvis.capabilities import CapabilityRegistry, Risk
from jarvis.capabilities.execute import ExecOutcome, execute, execute_approved
from jarvis.db import reset_for_tests as reset_db
from jarvis.events import EventType
from jarvis.events.bus import EventBus
from jarvis.policy import Autonomy, CallContext, Surface
from jarvis.policy import approvals as approval_store
from jarvis.scheduler import engine, task_store
from jarvis.scheduler.briefing import facts_to_prompt, gather_facts
from jarvis.scheduler.briefing_config import get_config, set_config
from jarvis.scheduler.recurrence import describe, next_run_at
from jarvis.tools import load_tools


@pytest.fixture(autouse=True)
def _isolate(scratch, monkeypatch):
    reset_db()
    monkeypatch.delenv(engine.ENABLE_ENV, raising=False)
    yield
    reset_db()


@pytest.fixture
def reg():
    registry = CapabilityRegistry()
    load_tools(registry)
    return registry


def ctx(turn="t1", **kw) -> CallContext:
    defaults = dict(session_id="s1", turn_id=turn, surface=Surface.TEXT,
                    autonomy=Autonomy.INTERACTIVE)
    defaults.update(kw)
    return CallContext(**defaults)


# --- recurrence --------------------------------------------------------------

MONDAY_9AM = datetime(2026, 9, 7, 9, 0)
SATURDAY_9AM = datetime(2026, 9, 5, 9, 0)


def test_a_time_already_past_today_means_tomorrow():
    assert next_run_at({"type": "daily", "time": "08:00"}, MONDAY_9AM) == \
        datetime(2026, 9, 8, 8, 0)


def test_a_time_still_to_come_means_today():
    assert next_run_at({"type": "daily", "time": "17:00"}, MONDAY_9AM) == \
        datetime(2026, 9, 7, 17, 0)


def test_a_weekday_schedule_skips_the_weekend():
    assert next_run_at({"type": "weekdays", "time": "08:00"}, SATURDAY_9AM) == \
        datetime(2026, 9, 7, 8, 0)


def test_a_weekly_schedule_picks_the_next_listed_day():
    # 3 = Wednesday.
    assert next_run_at({"type": "weekly", "time": "18:30", "days": [3]}, MONDAY_9AM) == \
        datetime(2026, 9, 9, 18, 30)


def test_a_one_off_in_the_past_never_runs_again():
    assert next_run_at({"type": "once", "at": "2020-01-01T00:00:00.000Z"}, MONDAY_9AM) is None


def test_an_interval_lands_on_its_own_grid():
    """From the anchor, not from now — otherwise every restart shifts the grid."""
    spec = {"type": "interval", "everyMs": 900000, "anchor": "2026-09-07T09:00:00"}
    assert next_run_at(spec, datetime(2026, 9, 7, 9, 20)) == datetime(2026, 9, 7, 9, 30)


def test_a_daily_schedule_stays_at_the_same_clock_time_across_a_dst_change():
    """The rollover re-pins the clock time after adding a day rather than adding
    24 hours, so an 8am alarm does not become 7am or 9am twice a year."""
    # 2026-03-08 is when US clocks go forward; the schedule must still say 08:00.
    result = next_run_at({"type": "daily", "time": "08:00"}, datetime(2026, 3, 7, 9, 0))
    assert (result.hour, result.minute) == (8, 0)


def test_a_schedule_describes_itself_in_words_someone_can_confirm():
    assert describe({"type": "weekdays", "time": "09:30"}) == "weekdays at 9:30 AM"
    assert describe({"type": "interval", "everyMs": 3600000}) == "every 1 hour"
    assert describe(None) == "never"


# --- the engine --------------------------------------------------------------

def test_the_timer_stays_off_unless_the_interlock_is_set():
    """Two schedulers on one tasks.json fire every task twice, silently."""
    assert engine.is_enabled() is False
    assert engine.start() is False


def test_the_timer_starts_when_it_is_allowed(monkeypatch):
    monkeypatch.setenv(engine.ENABLE_ENV, "1")
    try:
        assert engine.start() is True
    finally:
        engine.stop()


def test_a_due_task_runs_and_the_schedule_advances_first():
    task = task_store.create_task(title="Say hello", recurrence={"type": "daily", "time": "08:00"},
                                  action={"type": "message", "text": "hello"})
    task_store.update_task(task["id"], {"nextRunAt": "2020-01-01T00:00:00.000Z"})

    ran = engine.tick(now=MONDAY_9AM, event_bus=EventBus())
    assert ran == [task["id"]]

    after = task_store.get_task(task["id"])
    assert after["nextRunAt"] > "2026", "the schedule must advance before the run"
    assert task_store.list_runs(task["id"])[0]["ok"] is True


def test_a_one_off_disables_itself_rather_than_re_firing():
    task = task_store.create_task(recurrence={"type": "once", "at": "2020-01-01T00:00:00.000Z"},
                                  action={"type": "message", "text": "once only"})
    task_store.update_task(task["id"], {"nextRunAt": "2020-01-01T00:00:00.000Z"})

    engine.tick(now=MONDAY_9AM, event_bus=EventBus())
    after = task_store.get_task(task["id"])
    assert after["enabled"] is False and after["nextRunAt"] is None
    assert engine.tick(now=MONDAY_9AM, event_bus=EventBus()) == []


def test_a_missed_run_catches_up_once_and_is_reported_as_late():
    task = task_store.create_task(recurrence={"type": "daily", "time": "08:00"},
                                  action={"type": "message", "text": "hello"})
    task_store.update_task(task["id"], {"nextRunAt": "2020-01-01T00:00:00.000Z"})
    engine.tick(now=MONDAY_9AM, event_bus=EventBus())

    runs = task_store.list_runs(task["id"])
    assert len(runs) == 1 and runs[0]["late"] is True


def test_notify_never_stays_silent_and_on_error_speaks_only_on_failure():
    seen: list[dict] = []
    ebus = EventBus()
    ebus.subscribe(EventType.NOTIFICATION_CREATED, lambda e: seen.append(e.payload))

    quiet = task_store.create_task(recurrence={"type": "daily", "time": "08:00"},
                                   action={"type": "message", "text": "x"}, notify="never")
    normal = task_store.create_task(recurrence={"type": "daily", "time": "08:00"},
                                    action={"type": "message", "text": "x"}, notify="on_error")
    for task in (quiet, normal):
        task_store.update_task(task["id"], {"nextRunAt": "2020-01-01T00:00:00.000Z"})

    engine.tick(now=MONDAY_9AM, event_bus=ebus)
    assert seen == [], "a successful on_error run must not interrupt anyone"


def test_an_unknown_action_fails_the_run_rather_than_the_loop():
    task = task_store.create_task(recurrence={"type": "daily", "time": "08:00"},
                                  action={"type": "nonsense"})
    task_store.update_task(task["id"], {"nextRunAt": "2020-01-01T00:00:00.000Z"})
    engine.tick(now=MONDAY_9AM, event_bus=EventBus())
    run = task_store.list_runs(task["id"])[0]
    assert run["ok"] is False and "Unknown task action" in run["error"]


def test_run_history_is_capped():
    task = task_store.create_task(recurrence={"type": "daily", "time": "08:00"},
                                  action={"type": "message", "text": "x"})
    for _ in range(task_store.MAX_RUNS_KEPT + 5):
        task_store.record_run({"taskId": task["id"], "ok": True, "title": "x"})
    assert len(task_store.list_runs()) == task_store.MAX_RUNS_KEPT


# --- the briefing ------------------------------------------------------------

def test_the_briefing_prompt_forbids_inventing_anything():
    """A briefing that invents a meeting is worse than no briefing, because it
    is indistinguishable from a real one."""
    prompt = facts_to_prompt(gather_facts(get_config()), get_config())
    assert "ONLY facts you have" in prompt
    assert "never fill a gap" in prompt


def test_an_empty_schedule_is_stated_rather_than_left_open():
    prompt = facts_to_prompt(gather_facts(get_config()), get_config())
    assert "Scheduled: nothing." in prompt


def test_weather_is_skipped_until_a_place_is_known():
    assert "weather" not in gather_facts(get_config())
    set_config({"weatherPlace": "Lagos"})
    assert "weather" in gather_facts(get_config())


# --- the tools ---------------------------------------------------------------

def test_scheduling_reads_back_the_schedule_in_plain_english(reg):
    result = execute("schedule_task", {"when": "weekdays", "time": "09:30",
                                       "title": "Stand-up"}, ctx(), registry=reg)
    assert result.outcome is ExecOutcome.NEEDS_APPROVAL
    reason = approval_store.get(result.approval_id).reason
    assert 'Schedule "Stand-up" weekdays at 9:30 AM?' in reason
    assert task_store.list_tasks() == []


def test_an_unusable_schedule_is_explained_not_saved(reg):
    result = execute("schedule_task", {"when": "weekly", "time": "09:30"}, ctx(), registry=reg)
    assert "which days" in approval_store.get(result.approval_id).reason


def test_approving_actually_schedules_it(reg):
    result = execute("schedule_task", {"when": "daily", "time": "08:00", "text": "take meds"},
                     ctx("t1"), registry=reg)
    ran = execute_approved(result.approval_id, "t2", ctx("t2"), registry=reg)
    assert ran.ok and ran.value["schedule"] == "every day at 8:00 AM"
    assert len(task_store.list_tasks()) == 1


def test_listing_needs_no_approval(reg):
    task_store.create_task(title="Stand-up", recurrence={"type": "daily", "time": "09:30"},
                           action={"type": "message", "text": "x"})
    result = execute("list_tasks", {}, ctx(), registry=reg)
    assert result.ok and result.value["tasks"][0]["schedule"] == "every day at 9:30 AM"


def test_cancelling_names_what_will_stop(reg):
    task_store.create_task(title="Stand-up", recurrence={"type": "daily", "time": "09:30"},
                           action={"type": "message", "text": "x"})
    result = execute("cancel_task", {"query": "stand"}, ctx(), registry=reg)
    assert 'Cancel "Stand-up" (every day at 9:30 AM)?' in \
        approval_store.get(result.approval_id).reason
    assert len(task_store.list_tasks()) == 1, "nothing cancelled before the user answers"


def test_cancelling_something_that_does_not_exist_says_so(reg):
    result = execute("cancel_task", {"query": "the thing"}, ctx(), registry=reg)
    assert "couldn't find" in approval_store.get(result.approval_id).reason


def test_scheduling_and_cancelling_are_medium_and_listing_is_low(reg):
    assert reg.get("schedule_task").risk is Risk.MEDIUM
    assert reg.get("cancel_task").risk is Risk.MEDIUM
    assert reg.get("list_tasks").risk is Risk.LOW


# --- the rule the owner set, end to end --------------------------------------

def test_a_scheduled_task_may_do_ordinary_work_but_never_a_high_risk_action(scratch):
    """The owner's explicit decision: "keep HIGH-risk actions requiring fresh
    human confirmation even when they are triggered by scheduled tasks or
    briefings." Setting up a task is not consent for whatever it later decides
    to delete.

    Driven through the REAL task runner and a real model turn, because this is
    exactly the kind of rule that survives in a policy unit test and quietly
    stops applying at the one call site that matters.
    """
    from jarvis import assembly, conversation
    from jarvis.capabilities import CapabilitySpec
    from jarvis.gateway import availability, connections, registry as model_registry

    from stub_openai_server import StubModelServer

    assembly.reset_for_tests()
    conversation.reset_for_tests()
    availability.reset_for_tests()
    stub = StubModelServer()
    base = stub.start()
    try:
        conn = connections.add_connection(adapter="openai-compatible", base_url=base,
                                          label="stub", provider="custom", kind="local",
                                          key_required=False)
        model_registry.add_model(connection_id=conn["id"], model="stub-model")

        did = {"medium": 0, "high": 0}
        caps = assembly.get_registry()
        caps.register(CapabilitySpec(
            id="test.tidy", name="tidy_up", description="ordinary work",
            input_schema={"type": "object", "properties": {}},
            risk=Risk.MEDIUM, handler=lambda **_: did.__setitem__("medium", 1) or "tidied"))
        caps.register(CapabilitySpec(
            id="test.delete_everything", name="delete_everything", description="dangerous",
            input_schema={"type": "object", "properties": {}},
            risk=Risk.HIGH, handler=lambda **_: did.__setitem__("high", 1) or "gone"))

        # The ordinary action runs unattended; the dangerous one does not.
        stub.calls_tool("tidy_up", {}, call_id="c1")
        stub.calls_tool("delete_everything", {}, call_id="c2")
        stub.says("All done.")

        task = task_store.create_task(
            title="Nightly cleanup",
            recurrence={"type": "daily", "time": "03:00"},
            action={"type": "prompt", "text": "tidy up and then clear the old files"})
        result = engine.run_task_now(task["id"], event_bus=EventBus())

        assert did["medium"] == 1, "pre-consent covers ordinary work"
        assert did["high"] == 0, "a high-risk action must wait for a person"
        assert result["awaitingApproval"], "and the task must say it is waiting"

        run = task_store.list_runs(task["id"])[0]
        assert run["ok"] is False and "go-ahead" in run["error"]

        pending = approval_store.pending()
        assert [a.capability for a in pending] == ["delete_everything"]
        assert pending[0].surface == "scheduled"
    finally:
        stub.stop()
        assembly.reset_for_tests()
        availability.reset_for_tests()
        conversation.reset_for_tests()
