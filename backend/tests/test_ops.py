"""Operational awareness: diagnosis, environment, verification.

The tests that matter most here are the ones about NOT firing — a load spike
that is real but brief, a failure already reported last tick, a check that
cannot be run. Every one of those is a notification the owner would otherwise
get repeatedly for something they already know, which is how a channel that is
supposed to matter becomes one nobody reads.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from jarvis.db import reset_for_tests as reset_db
from jarvis.events import EventType
from jarvis.events.bus import Event, EventBus
from jarvis.heartbeat import outbox, schedule_store
from jarvis.jscompat import to_iso_z
from jarvis.ops import consequence, trace, verify
from jarvis.ops.diagnostics import registry as check_registry
from jarvis.ops.diagnostics.registry import Check
from jarvis.ops.environment import baseline, sampler
from jarvis.ops.environment import store as env_store


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    check_registry.reset()
    sampler.reset_for_tests()
    yield
    sampler.reset_for_tests()
    check_registry.reset()
    reset_db()


# --- the shared trace ---------------------------------------------------------

def test_two_subsystems_writing_the_trace_keep_independent_sequences():
    """A diagnosis and a job write to the same table; neither may perturb the
    other's numbering, or a recovery decision reads someone else's history."""
    from jarvis.jobs import job_store

    job = job_store.create_job(title="t", goal="g")
    job_store.append_trace(job["id"], phase="intent", effect="read", kind="note", summary="one")
    trace.append(source="diagnosis", source_ref="memory", phase="intent", effect="read",
                 kind="probe", summary="checking")
    job_store.append_trace(job["id"], phase="outcome", effect="read", kind="note", summary="two")

    assert [r["seq"] for r in job_store.get_trace(job["id"])] == [1, 2]
    assert [r["seq"] for r in trace.read("diagnosis", "memory")] == [1]


def test_a_trace_row_must_say_what_it_touched():
    """`effect` is what recovery classification reads. A typo that silently
    became 'read' would make an unrepeatable action look safe to retry."""
    with pytest.raises(ValueError):
        trace.append(source="x", source_ref="y", phase="intent", effect="side-effects",
                     kind="k", summary="s")
    with pytest.raises(ValueError):
        trace.append(source="x", source_ref="y", phase="during", effect="read",
                     kind="k", summary="s")


# --- system load: the point is what it does NOT report ------------------------

def _samples(values, *, now, step_s=30):
    """Newest first, `step_s` apart — the shape the store returns."""
    return [{"ts": to_iso_z(now - timedelta(seconds=step_s * i)), "cpuPct": v,
             "memFreePct": 50.0, "rssBytes": 1}
            for i, v in enumerate(values)]


def test_a_single_spike_is_not_a_finding():
    now = datetime.now(timezone.utc)
    quiet = [5.0] * 40
    samples = _samples([95.0] + quiet, now=now)
    assert baseline.check_for_anomaly(samples, now=now) is None


def test_sustained_high_load_is_a_finding_with_the_real_numbers_in_it():
    now = datetime.now(timezone.utc)
    # Twelve samples at 30s covers the five-minute requirement; the rest sets
    # this machine's own typical level.
    samples = _samples([90.0] * 12 + [5.0] * 40, now=now)
    finding = baseline.check_for_anomaly(samples, now=now)
    assert finding is not None
    assert "90%" in finding["summary"] and "5%" in finding["summary"]
    assert json.loads(finding["detail"])["kind"] == "cpu"


def test_a_near_idle_machine_never_reports_trivial_noise():
    """A median of 1% makes 1.5% "unusually high" arithmetically. It is not."""
    now = datetime.now(timezone.utc)
    samples = _samples([2.0] * 12 + [1.0] * 40, now=now)
    assert baseline.check_for_anomaly(samples, now=now) is None


def test_a_short_burst_that_has_not_lasted_long_enough_is_not_yet_a_finding():
    now = datetime.now(timezone.utc)
    # Elevated, but only across the last ninety seconds.
    samples = _samples([90.0] * 3 + [5.0] * 40, now=now)
    assert baseline.check_for_anomaly(samples, now=now) is None


def test_no_baseline_yet_means_silence_rather_than_a_guess():
    now = datetime.now(timezone.utc)
    assert baseline.check_for_anomaly(_samples([99.0] * 4, now=now), now=now) is None


def test_sustained_low_memory_is_reported_on_its_own():
    now = datetime.now(timezone.utc)
    samples = [{"ts": to_iso_z(now - timedelta(seconds=30 * i)), "cpuPct": 5.0,
                "memFreePct": 3.0, "rssBytes": 1} for i in range(20)]
    finding = baseline.check_for_anomaly(samples, now=now)
    assert finding is not None
    assert json.loads(finding["detail"])["kind"] == "memory"


def test_the_first_cpu_reading_is_unknown_rather_than_zero():
    first = sampler.read_now()
    assert first["cpuPct"] is None
    assert sampler.read_now()["cpuPct"] is not None


def test_the_sampler_stays_off_without_its_interlock(monkeypatch):
    monkeypatch.delenv(sampler.ENABLE_ENV, raising=False)
    assert sampler.start() is False


def test_a_recorded_sample_reads_back():
    sampler.take_sample()
    rows = env_store.list_recent()
    assert len(rows) == 1 and rows[0]["memFreePct"] is not None


# --- the environment source's own dedup ---------------------------------------

def test_the_same_ongoing_load_problem_is_reported_once_not_every_tick(monkeypatch):
    from jarvis.ops.environment import source as env_source

    anomaly = {"summary": "CPU is high", "detail": json.dumps({"kind": "cpu"})}
    monkeypatch.setattr(env_source.baseline, "check_for_anomaly", lambda *a, **k: anomaly)
    schedule_store.upsert_item(env_source.SOURCE_ID, "system-load", 1000)

    first = env_source.check("system-load")
    assert first["finding"] is not None
    schedule_store.mark_done(f"{env_source.SOURCE_ID}:system-load", first["checkState"])

    second = env_source.check("system-load")
    assert second["finding"] is None

    # ...and once it settles, the memory of the report is cleared, so a genuine
    # recurrence is reported again rather than suppressed forever.
    monkeypatch.setattr(env_source.baseline, "check_for_anomaly", lambda *a, **k: None)
    settled = env_source.check("system-load")
    assert settled["finding"] is None and settled["checkState"] is None
    schedule_store.mark_done(f"{env_source.SOURCE_ID}:system-load", settled["checkState"])

    monkeypatch.setattr(env_source.baseline, "check_for_anomaly", lambda *a, **k: anomaly)
    assert env_source.check("system-load")["finding"] is not None


# --- diagnosis: one remedy per new failure ------------------------------------

def _run_check(item_key):
    """One tick's worth of processing for a single check, schedule row and all."""
    from jarvis.ops.diagnostics import source

    row_id = f"{source.SOURCE_ID}:{item_key}"
    schedule_store.upsert_item(source.SOURCE_ID, item_key, 1000)
    result = source.check(item_key)
    schedule_store.mark_done(row_id, result["checkState"]) if "checkState" in result \
        else schedule_store.mark_done(row_id)
    return result


def test_a_check_that_heals_itself_never_reaches_the_user():
    state = {"broken": True, "remedies": 0}

    def probe():
        return {"ok": not state["broken"], "detail": "it broke"}

    def remedy():
        state["remedies"] += 1
        state["broken"] = False

    check_registry.register_check(Check(id="flaky", probe=probe, remedy=remedy))
    result = _run_check("flaky")
    assert result["finding"] is None
    assert state["remedies"] == 1
    assert any("fixed itself" in r["summary"] for r in trace.read("diagnosis", "flaky"))


def test_a_check_the_remedy_cannot_fix_escalates_once_and_then_stays_quiet():
    attempts = {"remedies": 0}

    def remedy():
        attempts["remedies"] += 1

    check_registry.register_check(Check(id="stuck", probe=lambda: {"ok": False, "detail": "no"},
                                        remedy=remedy))
    first = _run_check("stuck")
    assert first["finding"] is not None and attempts["remedies"] == 1

    second = _run_check("stuck")
    assert second["finding"] is None, "a failure already reported must not re-notify"
    assert attempts["remedies"] == 1, "a remedy is spent per NEW failure, never per tick"


def test_a_check_with_no_remedy_escalates_straight_away():
    check_registry.register_check(Check(id="watchful", kind="security",
                                        probe=lambda: {"ok": False, "detail": "odd"}))
    result = _run_check("watchful")
    assert result["finding"]["detail"] == "odd"


def test_a_security_check_may_not_declare_a_remedy():
    """Silently fixing a security anomaly destroys the evidence of one."""
    with pytest.raises(ValueError):
        check_registry.register_check(
            Check(id="s", kind="security", probe=lambda: {"ok": True}, remedy=lambda: None))


def test_a_probe_that_throws_is_a_failing_check_not_a_crashed_tick():
    def probe():
        raise RuntimeError("exploded")

    check_registry.register_check(Check(id="angry", probe=probe))
    result = _run_check("angry")
    assert result["finding"] is not None and "exploded" in result["finding"]["detail"]


def test_recovery_is_recorded_but_not_reported():
    state = {"broken": True}
    check_registry.register_check(Check(id="recovering",
                                        probe=lambda: {"ok": not state["broken"], "detail": "d"}))
    assert _run_check("recovering")["finding"] is not None
    state["broken"] = False
    assert _run_check("recovering")["finding"] is None
    assert any("working again" in r["summary"] for r in trace.read("diagnosis", "recovering"))


def test_the_real_checks_all_run_and_report_honestly():
    from jarvis.ops.diagnostics import checks

    checks.reset_for_tests()
    checks.register_all()
    ids = {c.id for c in check_registry.list_checks()}
    assert {"database", "memory", "jobs", "scheduler", "capture_health", "voice",
            "config_integrity", "event_spikes", "listeners"} <= ids

    for check in check_registry.list_checks():
        result = check.probe()
        assert isinstance(result.get("ok"), bool), f"{check.id} did not answer ok/not-ok"


def test_the_memory_canary_leaves_nothing_behind():
    from jarvis.memory import store as memory_store
    from jarvis.ops.diagnostics.checks import memory as memory_check

    before = len(memory_store.list_memories())
    assert memory_check.probe()["ok"] is True
    assert len(memory_store.list_memories()) == before


def test_the_config_check_notices_a_change_it_did_not_make(scratch):
    from jarvis.ops.diagnostics.checks.security import config_integrity

    scratch.env_path.write_text("OPENAI_API_KEY=abc\n", encoding="utf-8")
    # First sight: recorded, never reported — there is nothing to compare against.
    assert config_integrity.probe()["ok"] is True
    assert config_integrity.probe()["ok"] is True

    scratch.env_path.write_text("OPENAI_API_KEY=abc\nSOMETHING_ELSE=1\n", encoding="utf-8")
    result = config_integrity.probe()
    assert result["ok"] is False
    assert "abc" not in json.dumps(result), "a check on a secrets file must never quote it"


def test_a_write_this_app_made_itself_is_not_reported_as_a_change():
    from jarvis.config import save_secret
    from jarvis.ops.diagnostics.checks.security import config_integrity

    save_secret("thing", "value")
    assert config_integrity.probe()["ok"] is True
    save_secret("other", "value")
    assert config_integrity.probe()["ok"] is True


def test_the_listener_check_passes_for_a_loopback_only_app():
    from jarvis.ops.diagnostics.checks.security import listeners

    assert listeners.probe()["ok"] is True


# --- verification -------------------------------------------------------------

def test_a_missing_or_empty_file_fails_mechanically(tmp_path):
    assert verify.verify_file_opens(tmp_path / "nope.txt")["ok"] is False
    empty = tmp_path / "empty.txt"
    empty.write_text("")
    assert verify.verify_file_opens(empty)["ok"] is False


def test_a_format_nothing_can_reopen_is_not_reported_as_verified(tmp_path):
    plain = tmp_path / "notes.txt"
    plain.write_text("hello")
    result = verify.verify_file_opens(plain)
    assert result["ok"] is True and result["checked"] is False


def test_a_generated_document_is_verified_by_actually_reopening_it(tmp_path):
    from jarvis.artifacts import office

    path = tmp_path / "report.docx"
    office.write_docx(path, "A heading\n\nSome real body text.")
    result = verify.verify_file_opens(path)
    assert result == {"ok": True, "checked": True}

    # A file that merely claims to be one is caught, not trusted by extension.
    fake = tmp_path / "fake.docx"
    fake.write_bytes(b"not a zip at all")
    assert verify.verify_file_opens(fake)["ok"] is False


def test_a_code_run_is_judged_by_its_real_exit_signal():
    assert verify.verify_code_ran({"exit_code": 0})["ok"] is True
    assert verify.verify_code_ran({"exit_code": 1, "stderr": "boom"})["ok"] is False
    assert verify.verify_code_ran({"exit_code": 0, "timed_out": True})["ok"] is False
    assert verify.verify_code_ran(None)["ok"] is False


def test_no_model_available_is_never_reported_as_a_pass_or_a_fail(monkeypatch):
    def unavailable(*_a, **_kw):
        raise RuntimeError("everything is rate limited")

    monkeypatch.setattr("jarvis.gateway.client.ask", unavailable)
    verdict = verify.verify_semantic_match(request="r", result_summary="s")
    assert verdict.checked is False and verdict.matches is None and verdict.failed is False


def test_an_unreadable_reply_is_not_guessed_at(monkeypatch):
    class Answer:
        data = {"probably": "yes"}

    monkeypatch.setattr("jarvis.gateway.client.ask", lambda *a, **k: Answer())
    assert verify.verify_semantic_match(request="r", result_summary="s").checked is False


def test_a_real_mismatch_is_reported_as_one(monkeypatch):
    class Answer:
        data = {"matches": False, "reason": "it answered a different question"}

    monkeypatch.setattr("jarvis.gateway.client.ask", lambda *a, **k: Answer())
    verdict = verify.verify_semantic_match(request="r", result_summary="s")
    assert verdict.failed is True and verdict.reason


# --- was it worth checking at all --------------------------------------------

@pytest.mark.parametrize("answer,tools,artifact,expected", [
    ("yes", [], False, False),
    ("It is 4pm.", ["get_time"], False, False),
    ("x" * 300, [], False, True),
    ("short", ["send_email"], False, True),
    ("short", [], True, True),
    ("x" * 300, ["check_myself"], False, True),
])
def test_what_counts_as_worth_checking(answer, tools, artifact, expected):
    judgment = consequence.is_consequential(answer_text=answer, tool_names=tools,
                                            produced_artifact=artifact)
    assert judgment.worth_checking is expected
    assert judgment.reason


def test_the_daily_budget_is_real():
    for _ in range(consequence.DAILY_BUDGET):
        assert consequence.try_consume_budget() is True
    assert consequence.try_consume_budget() is False
    assert consequence.budget_remaining() == 0


# --- the observer: off by default --------------------------------------------

def _response_event(text="x" * 300, tools=("send_email",)):
    return Event(type=EventType.ASSISTANT_RESPONSE, payload={
        "sessionId": "s1", "turnId": "t1", "text": text,
        "userText": "do the thing", "toolNames": list(tools)})


def test_chat_answers_are_not_verified_unless_the_preference_is_on(monkeypatch):
    from jarvis.observers import verification

    called = []
    monkeypatch.setattr(verify, "verify_semantic_match",
                        lambda **kw: called.append(kw) or verify.Verdict(True, True))
    verification.verify_answer(_response_event())
    verification.join_all()
    assert called == []
    assert consequence.budget_remaining() == consequence.DAILY_BUDGET


def test_with_the_preference_on_a_mismatch_is_traced_and_raised(monkeypatch):
    from jarvis.observers import verification
    from jarvis.prefs import set_prefs

    set_prefs({"verifyChatAnswers": True})
    monkeypatch.setattr("jarvis.ops.verify.verify_semantic_match",
                        lambda **kw: verify.Verdict(True, False, "it answered something else"))

    verification.verify_answer(_response_event())
    verification.join_all()

    rows = trace.read("verification", "t1")
    assert len(rows) == 1 and "did NOT match" in rows[0]["summary"]
    parked = outbox.list_pending()
    assert len(parked) == 1 and parked[0]["tier"] == 2
    assert "may not have actually answered" in parked[0]["summary"]


def test_a_matching_answer_is_recorded_but_never_interrupts(monkeypatch):
    from jarvis.observers import verification
    from jarvis.prefs import set_prefs

    set_prefs({"verifyChatAnswers": True})
    monkeypatch.setattr("jarvis.ops.verify.verify_semantic_match",
                        lambda **kw: verify.Verdict(True, True, "yes"))
    verification.verify_answer(_response_event())
    verification.join_all()
    assert len(trace.read("verification", "t1")) == 1
    assert outbox.list_pending() == []


def test_a_check_that_could_not_run_records_nothing_at_all(monkeypatch):
    from jarvis.observers import verification
    from jarvis.prefs import set_prefs

    set_prefs({"verifyChatAnswers": True})
    monkeypatch.setattr("jarvis.ops.verify.verify_semantic_match",
                        lambda **kw: verify.Verdict(False, None))
    verification.verify_answer(_response_event())
    verification.join_all()
    assert trace.read("verification", "t1") == []
    assert outbox.list_pending() == []


def test_an_inconsequential_answer_is_not_checked_even_with_the_preference_on(monkeypatch):
    from jarvis.observers import verification
    from jarvis.prefs import set_prefs

    set_prefs({"verifyChatAnswers": True})
    called = []
    monkeypatch.setattr("jarvis.ops.verify.verify_semantic_match",
                        lambda **kw: called.append(kw) or verify.Verdict(True, True))
    verification.verify_answer(_response_event(text="yes", tools=()))
    verification.join_all()
    assert called == []


# --- the tools ----------------------------------------------------------------

def test_check_environment_reports_real_readings_and_no_invented_ones():
    from jarvis.tools.check_environment import SPEC

    answer = SPEC.handler()
    assert answer["ok"] is True
    assert answer["load"]["cpuCount"] >= 1
    # First reading of the process: honestly unknown.
    assert answer["load"]["cpuPct"] is None and "not known yet" in answer["load"]["note"]
    assert answer["unusual"] is None


def test_check_my_health_says_nothing_has_run_rather_than_claiming_health():
    from jarvis.ops.diagnostics import checks
    from jarvis.tools.check_my_health import SPEC

    checks.reset_for_tests()
    answer = SPEC.handler()
    assert answer["currentlyFailing"] == []
    assert "nothing to report" in answer["note"]


def test_check_my_health_reports_a_real_current_failure():
    from jarvis.ops.diagnostics import source

    check_registry.register_check(Check(id="broken", probe=lambda: {"ok": False, "detail": "d"}))
    _run_check("broken")

    health = source.recent_health()
    assert health["currentlyFailing"] == ["broken"]
    assert any("failing" in r["summary"] for r in health["recent"])


# --- verification wired where results are actually produced -------------------

def test_a_job_that_finishes_the_wrong_thing_is_treated_exactly_like_a_stall(monkeypatch):
    """No second recovery mechanism: a checked mismatch spends the same single
    retry, on the same counter, and escalates the same way a stall does."""
    from jarvis import assembly, conversation
    from jarvis.gateway import availability, connections
    from jarvis.gateway import registry as model_registry
    from jarvis.jobs import job_store, orchestrator, worker

    from stub_openai_server import StubModelServer

    stub = StubModelServer()
    base_url = stub.start()
    try:
        conn = connections.add_connection(adapter="openai-compatible", base_url=base_url,
                                          label="stub", provider="custom", kind="local",
                                          key_required=False)
        model_registry.add_model(connection_id=conn["id"], model="stub-model")
        monkeypatch.setattr("jarvis.ops.verify.verify_semantic_match",
                            lambda **kw: verify.Verdict(True, False, "it answered something else"))

        stub.says("All done, I think.")
        job = job_store.create_job(title="Do the thing", goal="do the thing")
        result = worker.run_job(job["id"], event_bus=EventBus())

        assert result["status"] == "stalled"
        assert "did not do what was asked" in job_store.get_job(job["id"])["error"]

        # ...and from there it is the ordinary one-retry-then-escalate path.
        stub.says("Done properly this time.")
        monkeypatch.setattr("jarvis.ops.verify.verify_semantic_match",
                            lambda **kw: verify.Verdict(True, True, "yes"))
        assert orchestrator.supervise(event_bus=EventBus())[0]["action"] == "retried"
        worker.join_all()
        assert job_store.get_job(job["id"])["status"] == "done"
    finally:
        worker.join_all()
        stub.stop()
        assembly.reset_for_tests()
        availability.reset_for_tests()
        conversation.reset_for_tests()
        stub.stop()


def test_a_job_whose_result_could_not_be_checked_still_completes(monkeypatch):
    from jarvis import assembly, conversation
    from jarvis.gateway import availability, connections
    from jarvis.gateway import registry as model_registry
    from jarvis.jobs import job_store, worker

    from stub_openai_server import StubModelServer

    stub = StubModelServer()
    base_url = stub.start()
    try:
        conn = connections.add_connection(adapter="openai-compatible", base_url=base_url,
                                          label="stub", provider="custom", kind="local",
                                          key_required=False)
        model_registry.add_model(connection_id=conn["id"], model="stub-model")
        monkeypatch.setattr("jarvis.ops.verify.verify_semantic_match",
                            lambda **kw: verify.Verdict(False, None))
        stub.says("Finished.")
        job = job_store.create_job(title="Do the thing", goal="do the thing")
        assert worker.run_job(job["id"], event_bus=EventBus())["status"] == "done"
    finally:
        worker.join_all()
        stub.stop()
        assembly.reset_for_tests()
        availability.reset_for_tests()
        conversation.reset_for_tests()


def test_a_scheduled_prompt_task_whose_answer_misses_the_point_is_recorded_as_not_ok(monkeypatch):
    from jarvis.scheduler import engine, task_store

    task = task_store.create_task(title="Morning summary", recurrence={"type": "daily", "time": "07:00"},
                                  action={"type": "prompt", "prompt": "summarise my day"})
    monkeypatch.setattr(engine, "_run_action",
                        lambda t: {"ok": True, "summary": "Here is a recipe for soup."})
    monkeypatch.setattr("jarvis.ops.verify.verify_semantic_match",
                        lambda **kw: verify.Verdict(True, False, "it is about soup"))

    result = engine.run_task_now(task["id"], event_bus=EventBus())
    assert result["ok"] is False
    assert "did not match" in result["error"]
    assert task_store.list_runs(task["id"])[0]["ok"] is False


def test_a_scheduled_task_of_another_kind_is_never_checked(monkeypatch):
    from jarvis.scheduler import engine, task_store

    called = []
    task = task_store.create_task(title="Open the thing", recurrence={"type": "daily", "time": "07:00"},
                                  action={"type": "capability", "name": "get_time", "args": {}})
    monkeypatch.setattr(engine, "_run_action", lambda t: {"ok": True, "summary": "opened"})
    monkeypatch.setattr("jarvis.ops.verify.verify_semantic_match",
                        lambda **kw: called.append(kw) or verify.Verdict(True, False, "no"))
    assert engine.run_task_now(task["id"], event_bus=EventBus())["ok"] is True
    assert called == []
