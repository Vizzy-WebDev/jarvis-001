"""Noticing things, and deciding whether to say anything.

Most of these are about restraint. The failure mode this subsystem has to avoid
is not missing something — it is saying the same thing every few minutes until
the person stops reading anything it says. So the tests are largely about a
condition that is still true producing nothing the second time, about a failed
judgment landing on silence rather than urgency, and about quiet hours holding.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from jarvis.db import reset_for_tests as reset_db
from jarvis.events import EventType
from jarvis.events.bus import EventBus
from jarvis.heartbeat import engine, outbox, quiet_hours, schedule_store
from jarvis.heartbeat.decision import Verdict
from jarvis.heartbeat.sources import registry as source_registry
from jarvis.heartbeat.sources.registry import Source
from jarvis.jscompat import to_iso_z


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    source_registry.reset()
    yield
    source_registry.reset()
    reset_db()


@pytest.fixture
def tier3(monkeypatch):
    """The default for these tests: a judgment that records and never interrupts,
    so a test about scheduling is not also a test about speaking."""
    monkeypatch.setattr(engine, "decide_attention",
                        lambda finding, **kw: Verdict(tier=3, reason="worth a record"))


def counting_source(source_id="s", items=("a",), finding=None, interval_ms=60_000):
    calls = {"checked": []}

    def check(item_key):
        calls["checked"].append(item_key)
        return {"finding": finding}

    source = Source(id=source_id, default_interval_ms=interval_ms,
                    list_items=lambda: [{"itemKey": k} for k in items], check=check)
    return source, calls


# --- the schedule -------------------------------------------------------------

def test_a_new_item_is_due_immediately_but_keeps_its_own_time_afterwards():
    """Something just started being watched deserves a first look now. Re-listing
    it must not reset the clock, or nothing would ever settle into a cadence."""
    first = schedule_store.upsert_item("s", "a", 60_000)
    assert schedule_store.list_due() == [first]

    schedule_store.mark_done(first["id"])
    assert schedule_store.list_due() == []

    schedule_store.upsert_item("s", "a", 60_000)
    assert schedule_store.list_due() == [], "re-registering must not make it due again"


def test_changing_an_interval_does_not_trigger_a_burst_of_rechecks():
    item = schedule_store.upsert_item("s", "a", 60_000)
    schedule_store.mark_done(item["id"])
    schedule_store.upsert_item("s", "a", 5_000)
    assert schedule_store.list_due() == []
    assert schedule_store.get_item("s", "a")["intervalMs"] == 5_000


def test_a_big_backlog_is_spread_across_ticks_and_nothing_is_done_twice(tier3):
    source, calls = counting_source(items=[f"item-{i}" for i in range(25)])
    source_registry.register(source)

    first = engine.tick(event_bus=EventBus())
    second = engine.tick(event_bus=EventBus())

    assert len(first) == engine.PER_TICK_CAP
    assert len(second) == 25 - engine.PER_TICK_CAP
    assert sorted(calls["checked"]) == sorted(f"item-{i}" for i in range(25))
    assert len(set(calls["checked"])) == 25, "an item was processed twice"


def test_an_item_still_being_checked_is_never_started_again():
    item = schedule_store.upsert_item("s", "a", 60_000)
    schedule_store.mark_running(item["id"])
    assert schedule_store.list_due() == []

    # ...and a row left running by a crash is cleared at startup, not mid-run.
    assert schedule_store.reset_stale_running() == 1
    assert len(schedule_store.list_due()) == 1


def test_items_a_source_no_longer_watches_are_dropped(tier3):
    watching = {"keys": ["a", "b"]}
    source = Source(id="s", default_interval_ms=60_000,
                    list_items=lambda: [{"itemKey": k} for k in watching["keys"]],
                    check=lambda key: {"finding": None})
    source_registry.register(source)

    engine.tick(event_bus=EventBus())
    assert len(schedule_store.list_for_source("s")) == 2

    watching["keys"] = ["a"]
    engine.tick(event_bus=EventBus())
    assert [i["itemKey"] for i in schedule_store.list_for_source("s")] == ["a"]


# --- isolation ----------------------------------------------------------------

def test_a_source_that_cannot_list_its_items_does_not_stop_another(tier3):
    def explode():
        raise RuntimeError("this source is broken")

    source_registry.register(Source(id="broken", default_interval_ms=60_000,
                                    list_items=explode, check=lambda k: {"finding": None}))
    healthy, calls = counting_source(source_id="healthy", items=("a",))
    source_registry.register(healthy)

    engine.tick(event_bus=EventBus())
    assert calls["checked"] == ["a"]


def test_a_check_that_throws_still_advances_its_own_schedule(tier3):
    def explode(_key):
        raise RuntimeError("check failed")

    source_registry.register(Source(id="s", default_interval_ms=60_000,
                                    list_items=lambda: [{"itemKey": "a"}], check=explode))
    result = engine.tick(event_bus=EventBus())
    assert result[0]["error"] is True
    assert schedule_store.list_due() == [], "a failing check must not retry every tick"


# --- the dedup that was learned the hard way ----------------------------------

def test_a_still_true_tier_three_finding_is_not_reported_on_every_tick(monkeypatch):
    """The real bug this contract exists to prevent: a tier-3 verdict creates no
    outbox row, so the broker's own duplicate check cannot see it, and one
    unanswered situation re-notified every few minutes forever."""
    judged = []
    monkeypatch.setattr(engine, "decide_attention",
                        lambda finding, **kw: judged.append(finding) or Verdict(3, "noted"))

    reported = {"count": 0}

    def check(item_key):
        from jarvis.heartbeat import schedule_store as store
        state = (store.get_item("s", item_key) or {}).get("checkState") or {}
        if state.get("reported"):
            return {"finding": None}     # the same situation, already reported
        reported["count"] += 1
        return {"finding": {"summary": "still true"}, "checkState": {"reported": True}}

    source_registry.register(Source(id="s", default_interval_ms=1,
                                    list_items=lambda: [{"itemKey": "a"}], check=check))

    for _ in range(5):
        engine.tick(event_bus=EventBus())

    assert reported["count"] == 1
    assert len(judged) == 1, "a still-true condition must not re-spend a model call"


def test_an_undelivered_row_stops_the_same_finding_being_parked_twice(monkeypatch):
    monkeypatch.setattr(engine, "decide_attention",
                        lambda finding, **kw: Verdict(2, "worth mentioning"))
    finding = {"summary": "something happened"}
    first = engine.route_finding("s", "a", finding, event_bus=EventBus())
    second = engine.route_finding("s", "a", finding, event_bus=EventBus())
    assert first["routed"] is True and second["routed"] is False
    assert len(outbox.list_pending()) == 1


# --- the judgment -------------------------------------------------------------

def test_the_record_is_written_whatever_the_tier(monkeypatch):
    bus = EventBus()
    seen = []
    bus.subscribe(EventType.NOTIFICATION_CREATED, seen.append)
    monkeypatch.setattr(engine, "decide_attention", lambda f, **kw: Verdict(3, "just a record"))

    engine.route_finding("s", "a", {"summary": "noticed something"}, event_bus=bus)
    assert len(seen) == 1 and seen[0].payload["meta"]["tier"] == 3
    assert outbox.list_pending() == [], "tier 3 never interrupts"


def test_a_failed_judgment_is_recorded_quietly_rather_than_treated_as_urgent(monkeypatch):
    def explode(_finding, **_kw):
        raise RuntimeError("no model")

    monkeypatch.setattr(engine, "decide_attention", explode)
    result = engine.route_finding("s", "a", {"summary": "something"}, event_bus=EventBus())
    assert result["tier"] == 3 and outbox.list_pending() == []


def test_no_model_available_is_tier_three_not_tier_one(monkeypatch):
    from jarvis.heartbeat import decision

    def unavailable(*_a, **_kw):
        raise RuntimeError("everything is rate limited")

    monkeypatch.setattr("jarvis.gateway.client.ask", unavailable)
    verdict = decision.decide_attention({"summary": "something"})
    assert verdict.tier == 3 and verdict.emergency is False


def test_an_unreadable_verdict_is_not_guessed_at(monkeypatch):
    from jarvis.heartbeat import decision

    class Answer:
        data = "not json at all"

    monkeypatch.setattr("jarvis.gateway.client.ask", lambda *a, **k: Answer())
    assert decision.decide_attention({"summary": "s"}).tier == 3


def test_an_emergency_flag_means_nothing_outside_quiet_hours(monkeypatch):
    from jarvis.heartbeat import decision

    class Answer:
        data = {"tier": 1, "reason": "urgent", "emergency": True, "emergencyReason": "money"}

    monkeypatch.setattr("jarvis.gateway.client.ask", lambda *a, **k: Answer())
    monkeypatch.setattr(decision, "is_quiet_now", lambda *a, **k: False, raising=False)
    monkeypatch.setattr("jarvis.heartbeat.quiet_hours.is_quiet_now", lambda *a, **k: False)
    verdict = decision.decide_attention({"summary": "s"})
    assert verdict.tier == 1 and verdict.emergency is False


# --- quiet hours --------------------------------------------------------------

@pytest.mark.parametrize("hour,expected", [(23, True), (2, True), (7, True), (8, False),
                                           (12, False), (22, False)])
def test_a_window_wrapping_midnight(hour, expected):
    window = {"enabled": True, "start": "23:00", "end": "08:00"}
    assert quiet_hours.is_quiet_now(datetime(2026, 3, 1, hour, 0), window) is expected


@pytest.mark.parametrize("window", [
    {"enabled": False, "start": "23:00", "end": "08:00"},
    {"enabled": True, "start": "nonsense", "end": "08:00"},
    {"enabled": True, "start": "23:00", "end": "23:00"},
    {},
])
def test_a_setting_that_cannot_be_read_never_silently_suppresses_everything(window):
    assert quiet_hours.is_quiet_now(datetime(2026, 3, 1, 2, 0), window) is False


def test_quiet_hours_hold_back_speech_but_never_the_record(monkeypatch):
    monkeypatch.setattr(engine, "decide_attention",
                        lambda f, **kw: Verdict(1, "urgent", emergency=False))
    monkeypatch.setattr("jarvis.heartbeat.quiet_hours.is_quiet_now", lambda *a, **k: True)
    spoke = []
    monkeypatch.setattr("jarvis.heartbeat.speak.speak_now",
                        lambda *a, **kw: spoke.append(a))

    result = engine.route_finding("s", "a", {"summary": "the roof is on fire"},
                                  event_bus=EventBus())
    assert result["spoken"] is False
    assert len(outbox.list_pending()) == 1, "the record is written regardless"
    assert spoke == []


def test_a_genuine_emergency_breaks_through_quiet_hours_if_they_are_reachable(monkeypatch):
    monkeypatch.setattr(engine, "decide_attention",
                        lambda f, **kw: Verdict(1, "urgent", emergency=True,
                                                emergency_reason="real money"))
    monkeypatch.setattr("jarvis.heartbeat.quiet_hours.is_quiet_now", lambda *a, **k: True)
    monkeypatch.setattr("jarvis.heartbeat.presence.is_reachable", lambda *a, **k: True)
    spoke = []
    monkeypatch.setattr("jarvis.heartbeat.speak.speak_now",
                        lambda *a, **kw: spoke.append(kw))

    assert engine.route_finding("s", "a", {"summary": "money is leaving"},
                                event_bus=EventBus())["spoken"] is True
    assert spoke and spoke[0]["reason"] == "urgent"


def test_nobody_there_means_nothing_is_said_even_in_an_emergency(monkeypatch):
    monkeypatch.setattr(engine, "decide_attention",
                        lambda f, **kw: Verdict(1, "urgent", emergency=True,
                                                emergency_reason="real"))
    monkeypatch.setattr("jarvis.heartbeat.quiet_hours.is_quiet_now", lambda *a, **k: True)
    monkeypatch.setattr("jarvis.heartbeat.presence.is_reachable", lambda *a, **k: False)
    assert engine.route_finding("s", "a", {"summary": "x"},
                                event_bus=EventBus())["spoken"] is False


def test_being_busy_holds_back_an_ordinary_tier_one_but_not_an_emergency(monkeypatch):
    monkeypatch.setattr("jarvis.heartbeat.quiet_hours.is_quiet_now", lambda *a, **k: False)
    monkeypatch.setattr("jarvis.heartbeat.presence.is_reachable", lambda *a, **k: True)
    monkeypatch.setattr("jarvis.heartbeat.presence.is_busy", lambda: True)
    monkeypatch.setattr(engine, "decide_attention", lambda f, **kw: Verdict(1, "important"))
    assert engine.route_finding("s", "a", {"summary": "x"},
                                event_bus=EventBus())["spoken"] is False


# --- presence -----------------------------------------------------------------

def test_reachable_needs_both_a_connected_ui_and_a_recently_active_person():
    from jarvis import chat_store
    from jarvis.events import bus
    from jarvis.heartbeat import presence

    assert presence.is_reachable() is False, "no UI attached"

    queue = bus.subscribe_queue()
    try:
        assert presence.is_reachable() is False, "a tab with no recent activity is not a person"

        conversation = chat_store.create_conversation()
        chat_store.set_active_id(conversation["id"])
        chat_store.append_message(conversation["id"], {"role": "user", "text": "hello"})
        assert presence.is_reachable() is True
    finally:
        bus.unsubscribe_queue(queue)


def test_a_tab_left_open_overnight_is_not_a_person_in_the_room():
    from jarvis import chat_store
    from jarvis.events import bus
    from jarvis.heartbeat import presence

    queue = bus.subscribe_queue()
    try:
        conversation = chat_store.create_conversation()
        chat_store.set_active_id(conversation["id"])
        chat_store.append_message(conversation["id"], {"role": "user", "text": "hello"})
        later = datetime.now(timezone.utc) + timedelta(hours=9)
        assert presence.is_reachable(now=later) is False
    finally:
        bus.unsubscribe_queue(queue)


# --- speaking first -----------------------------------------------------------

def test_speaking_first_puts_a_real_message_in_the_conversation(monkeypatch):
    from jarvis import conversation
    from jarvis.heartbeat.speak import speak_now

    bus = EventBus()
    heard = []
    bus.subscribe(EventType.ASSISTANT_RESPONSE, heard.append)

    entry_id = outbox.add(tier=1, summary="something", source="heartbeat", source_ref="s:a")
    session_id = speak_now("Your parcel is about to be sent back.", outbox_id=entry_id,
                           reason="it is time-limited", event_bus=bus)

    messages = conversation.get_messages(session_id)
    assert messages[-1]["role"] == "assistant"
    assert "parcel" in messages[-1]["text"]
    assert heard[0].payload["proactive"] is True
    # Actually saying it out loud IS the resolving action for this path.
    assert outbox.get(entry_id)["deliveredAt"] is not None
    conversation.reset_for_tests()


# --- the sources --------------------------------------------------------------

def test_the_jobs_source_watches_only_what_is_actually_waiting_on_the_user():
    from jarvis.heartbeat.sources import jobs_source
    from jarvis.jobs import job_store

    quiet = job_store.create_job(title="quiet", goal="g")
    waiting = job_store.create_job(title="needs a decision", goal="g")
    job_store.update_job(waiting["id"], {"status": "awaiting_decision"})
    job_store.add_outbox(tier=1, job_id=waiting["id"], reason="permission",
                         summary="may I send it?")
    job_store.add_outbox(tier=3, job_id=quiet["id"], reason="finished", summary="done")

    assert [i["itemKey"] for i in jobs_source.list_items()] == [waiting["id"]]


def test_the_same_unanswered_job_question_is_reported_once(monkeypatch):
    from jarvis.heartbeat.sources import jobs_source
    from jarvis.jobs import job_store

    job = job_store.create_job(title="needs a decision", goal="g")
    job_store.update_job(job["id"], {"status": "awaiting_decision"})
    job_store.add_outbox(tier=1, job_id=job["id"], reason="permission", summary="may I?")
    schedule_store.upsert_item(jobs_source.SOURCE_ID, job["id"], 1000)

    first = jobs_source.check(job["id"])
    assert first["finding"] is not None
    schedule_store.mark_done(f"{jobs_source.SOURCE_ID}:{job['id']}", first["checkState"])
    assert jobs_source.check(job["id"])["finding"] is None

    # A genuinely NEW ask is reported, though.
    job_store.add_outbox(tier=1, job_id=job["id"], reason="permission", summary="and this?")
    job_store.mark_delivered(job_store.list_pending_outbox()[0]["id"])
    assert jobs_source.check(job["id"])["finding"] is not None


def test_a_commitment_is_mentioned_once_as_it_approaches_and_once_when_it_passes():
    from jarvis.heartbeat.sources import commitments_source
    from jarvis.memory import store as memory_store

    now = datetime.now(timezone.utc)
    memory = memory_store.create_memory(
        category="About You", text="passport renewal is due",
        expires_at=to_iso_z(now + timedelta(hours=6)))
    key = memory["id"]
    schedule_store.upsert_item(commitments_source.SOURCE_ID, key, 1000)

    first = commitments_source.check(key, now=now)
    assert "Coming up" in first["finding"]["summary"]
    schedule_store.mark_done(f"{commitments_source.SOURCE_ID}:{key}", first["checkState"])
    assert commitments_source.check(key, now=now)["finding"] is None

    passed = now + timedelta(hours=7)
    lapsed = commitments_source.check(key, now=passed)
    assert "has now passed" in lapsed["finding"]["summary"]
    schedule_store.mark_done(f"{commitments_source.SOURCE_ID}:{key}", lapsed["checkState"])
    assert commitments_source.check(key, now=passed)["finding"] is None


def test_something_far_off_is_not_mentioned_yet():
    from jarvis.heartbeat.sources import commitments_source
    from jarvis.memory import store as memory_store

    now = datetime.now(timezone.utc)
    memory = memory_store.create_memory(category="About You", text="the trip",
                                        expires_at=to_iso_z(now + timedelta(days=9)))
    assert commitments_source.check(memory["id"], now=now)["finding"] is None


def test_a_memory_with_no_date_is_not_a_commitment():
    from jarvis.heartbeat.sources import commitments_source
    from jarvis.memory import store as memory_store

    memory_store.create_memory(category="About You", text="likes strong coffee")
    assert commitments_source.list_items() == []


# --- the trigger --------------------------------------------------------------

def test_a_job_parking_is_reacted_to_immediately_rather_than_at_the_next_poll(monkeypatch):
    from jarvis.heartbeat import triggers
    from jarvis.jobs import job_store

    bus = EventBus()
    routed = []
    monkeypatch.setattr("jarvis.heartbeat.engine.route_finding",
                        lambda *a, **kw: routed.append(a))
    triggers.start_triggers(bus)
    try:
        job = job_store.create_job(title="needs a decision", goal="g")
        job_store.update_job(job["id"], {"status": "awaiting_decision"})
        job_store.add_outbox(tier=1, job_id=job["id"], reason="permission", summary="may I?")

        bus.publish(EventType.JOB_UPDATED, {"id": job["id"], "status": "awaiting_decision"})
        assert len(routed) == 1

        # The trigger must persist its own dedup state, or the next ordinary
        # tick reports the same ask again.
        bus.publish(EventType.JOB_UPDATED, {"id": job["id"], "status": "awaiting_decision"})
        assert len(routed) == 1
    finally:
        triggers.stop_triggers()


def test_an_ordinary_status_change_is_not_a_trigger(monkeypatch):
    from jarvis.heartbeat import triggers

    bus = EventBus()
    routed = []
    monkeypatch.setattr("jarvis.heartbeat.engine.route_finding",
                        lambda *a, **kw: routed.append(a))
    triggers.start_triggers(bus)
    try:
        bus.publish(EventType.JOB_UPDATED, {"id": "j1", "status": "running"})
        assert routed == []
    finally:
        triggers.stop_triggers()


# --- reaching the user --------------------------------------------------------

def test_a_waiting_notice_reaches_a_turn_the_user_started_but_not_a_background_one():
    from jarvis.orchestrator.context import RelevanceContext

    outbox.add(tier=2, summary="your parcel is going back tomorrow", source="heartbeat",
               source_ref="s:a")
    assembler = RelevanceContext()

    live = assembler.assemble(session_id="s1", text="hello")
    assert "parcel" in live.system and "waiting_notices" in live.included

    background = assembler.assemble(session_id="s1", text="hello", background=True)
    assert "parcel" not in background.system


def test_a_notice_is_not_marked_delivered_merely_by_being_shown():
    from jarvis.orchestrator.context import RelevanceContext

    entry_id = outbox.add(tier=2, summary="something", source="heartbeat", source_ref="s:a")
    RelevanceContext().assemble(session_id="s1", text="hello")
    assert outbox.get(entry_id)["deliveredAt"] is None


def test_acknowledging_a_notice_is_what_marks_it_delivered():
    from jarvis.tools.acknowledge_notice import SPEC

    entry_id = outbox.add(tier=2, summary="something", source="heartbeat", source_ref="s:a")
    assert SPEC.handler(notice_id=entry_id)["ok"] is True
    assert outbox.get(entry_id)["deliveredAt"] is not None
    assert SPEC.handler(notice_id=entry_id)["alreadyDone"] is True


def test_a_job_notice_is_refused_because_it_has_its_own_resolving_actions():
    from jarvis.jobs import job_store
    from jarvis.tools.acknowledge_notice import SPEC

    job = job_store.create_job(title="t", goal="g")
    entry_id = job_store.add_outbox(tier=1, job_id=job["id"], reason="permission",
                                    summary="may I?")
    result = SPEC.handler(notice_id=entry_id)
    assert result["ok"] is False and "check_on_work" in result["error"]
    assert outbox.get(entry_id)["deliveredAt"] is None


def test_an_unknown_notice_is_refused_rather_than_silently_accepted():
    from jarvis.tools.acknowledge_notice import SPEC

    assert SPEC.handler(notice_id=999)["ok"] is False
    assert SPEC.handler(notice_id="not a number")["ok"] is False


# --- the interlock ------------------------------------------------------------

def test_the_heartbeat_stays_off_without_its_interlock(monkeypatch):
    monkeypatch.delenv(engine.ENABLE_ENV, raising=False)
    assert engine.start() is False
