"""Watching for something (§—), with only the conditions that genuinely work.

The most important test here is the refusal: a watch that can never fire is
worse than no watch, because the user believes it is running.
"""

from __future__ import annotations

import pytest

from jarvis.capabilities import CapabilityRegistry, Risk
from jarvis.capabilities.execute import ExecOutcome, execute
from jarvis.db import reset_for_tests as reset_db
from jarvis.events import EventType
from jarvis.events.bus import EventBus
from jarvis.monitor import engine, store
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


def ctx(turn="t1") -> CallContext:
    return CallContext(session_id="s1", turn_id=turn, surface=Surface.TEXT,
                       autonomy=Autonomy.INTERACTIVE)


# --- what it will and will not agree to watch --------------------------------

def test_a_desktop_condition_is_refused_with_a_reason(reg):
    """Not "unsupported": which thing, and why, and what it CAN do instead."""
    from jarvis.tools.monitor_tools import _watch

    result = _watch(description="Chrome opening", kind="window_appears")
    assert result["ok"] is False
    assert "window_appears" in result["error"]
    assert "file appearing" in result["error"]
    assert store.list_watching() == []


def test_an_unknown_condition_is_refused_too():
    with pytest.raises(engine.UnsupportedCheck):
        engine.validate({"kind": "the_stars_aligning"})


def test_a_file_watch_needs_a_file(reg):
    from jarvis.tools.monitor_tools import _watch

    assert _watch(description="the export", kind="file_exists")["ok"] is False


# --- the conditions that do work ---------------------------------------------

def test_a_file_appearing_triggers_once(tmp_path):
    target = tmp_path / "export.csv"
    monitor = store.create_monitor(description="the export finishing",
                                   check={"kind": "file_exists", "path": str(target)},
                                   on_trigger={"type": "notify"})
    assert engine.check_all(EventBus()) == []

    target.write_text("done")
    fired = engine.check_all(EventBus())
    assert [m["id"] for m in fired] == [monitor["id"]]
    # And stops watching, rather than announcing it again on every pass.
    assert engine.check_all(EventBus()) == []


def test_a_file_still_being_written_is_not_finished(tmp_path):
    """One reading cannot tell "finished" from "not started" — it takes two that
    agree."""
    target = tmp_path / "big.zip"
    target.write_bytes(b"x" * 100)
    store.create_monitor(description="the download finishing",
                         check={"kind": "file_size_stable", "path": str(target)},
                         on_trigger={"type": "notify"})

    assert engine.check_all(EventBus()) == [], "the first reading can never trigger"
    target.write_bytes(b"x" * 200)
    assert engine.check_all(EventBus()) == [], "still growing"
    assert len(engine.check_all(EventBus())) == 1, "two equal readings means finished"


def test_a_triggered_watch_announces_itself(tmp_path):
    seen: list[dict] = []
    ebus = EventBus()
    ebus.subscribe(EventType.NOTIFICATION_CREATED, lambda e: seen.append(e.payload))

    target = tmp_path / "here.txt"
    target.write_text("x")
    store.create_monitor(description="the file arriving",
                         check={"kind": "file_exists", "path": str(target)},
                         on_trigger={"type": "notify", "text": "it's here"})
    engine.check_all(ebus)

    assert seen and "the file arriving" in seen[0]["title"]


def test_one_broken_watch_does_not_stop_the_others(tmp_path, monkeypatch):
    target = tmp_path / "ok.txt"
    target.write_text("x")
    store.create_monitor(description="broken one", check={"kind": "file_exists"},
                         on_trigger={"type": "notify"})
    store.create_monitor(description="working one",
                         check={"kind": "file_exists", "path": str(target)},
                         on_trigger={"type": "notify"})

    real = engine.evaluate

    def explode(monitor):
        if monitor["description"] == "broken one":
            raise RuntimeError("boom")
        return real(monitor)

    monkeypatch.setattr(engine, "evaluate", explode)
    fired = engine.check_all(EventBus())
    assert [m["description"] for m in fired] == ["working one"]


# --- the tools ---------------------------------------------------------------

def test_setting_a_watch_is_read_back_first(reg, tmp_path):
    result = execute("watch_for", {"description": "the export finishing",
                                   "kind": "file_exists", "path": str(tmp_path / "x.csv")},
                     ctx(), registry=reg)
    assert result.outcome is ExecOutcome.NEEDS_APPROVAL
    assert "the export finishing" in approval_store.get(result.approval_id).reason
    assert store.list_watching() == []


def test_watching_and_stopping_are_both_medium(reg):
    """Silently dropping a watch someone is relying on is a loss they only find
    out about by the thing not happening."""
    assert reg.get("watch_for").risk is Risk.MEDIUM
    assert reg.get("stop_watching").risk is Risk.MEDIUM


def test_stopping_something_that_is_not_being_watched_says_so(reg):
    from jarvis.tools.monitor_tools import _stop

    assert _stop(which="the thing")["ok"] is False
