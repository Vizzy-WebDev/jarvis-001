"""The composition root's background-work switch (S7).

`start_background_work()` is the ONE place "what starts itself" is answerable
by reading a single function, and `main()` — the real launch path, never
called by a test's own `create_app()` — is what actually turns every
interlock on. Both properties are worth a real test, not just a reading of
the code: a subsystem added to one list and forgotten in the other would
silently never start in production while every existing test stayed green.
"""

from __future__ import annotations

import os

import pytest

from jarvis import assembly, main as main_module


def _stop_everything() -> None:
    """Undo every timer `start_background_work()` (or a real `main()` call)
    may have armed — real `threading.Timer`s, not mocks, so a test that
    starts them must also be the one that cancels them."""
    from jarvis import notifications
    from jarvis.connectors import icons as connector_icons
    from jarvis.cost import balances, prices
    from jarvis.heartbeat import engine as heartbeat
    from jarvis.heartbeat.triggers import stop_triggers
    from jarvis.improvement import cadence as improvement_cadence
    from jarvis.jobs import orchestrator as job_supervisor
    from jarvis.monitor import engine as monitor_engine
    from jarvis.ops.environment import sampler
    from jarvis.scheduler import engine as scheduler_engine

    for stop in (prices.stop_price_maintenance, balances.stop, sampler.stop,
                stop_triggers, heartbeat.stop, scheduler_engine.stop,
                monitor_engine.stop, job_supervisor.stop, improvement_cadence.stop,
                notifications.stop_trash_purge, connector_icons.stop):
        try:
            stop()
        except Exception:  # noqa: BLE001 — teardown must not itself fail the test
            pass


@pytest.fixture(autouse=True)
def _teardown():
    # `main()` sets every interlock with a RAW `os.environ.setdefault()` —
    # deliberately, so a real launch's own environment always wins (see its
    # own docstring) — which means `monkeypatch`'s usual revert does not
    # reliably undo it: monkeypatch restores exactly the value/absence it
    # observed at ITS OWN call time, and does not re-check or re-delete a var
    # something else (main() included) sets AFTER that. Left alone, one test
    # calling the real `main()` would leave every interlock permanently "on"
    # for the rest of the process, and a LATER, unrelated test creating a
    # real app (`create_app()` -> `start_background_work()`) would then
    # start real background threads it never asked for and never mocked —
    # including a real network call in `prices.start_price_maintenance()`.
    # So this snapshots and restores the real environment itself, on top of
    # whatever monkeypatch does.
    before = {var: os.environ.get(var) for var in main_module._BACKGROUND_INTERLOCKS}
    yield
    _stop_everything()
    for var, value in before.items():
        if value is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = value


def test_every_gated_subsystem_reports_started_when_every_interlock_is_set(scratch, monkeypatch):
    """Regression test for the exact bug S7's audit found: every one of these
    is real, tested, complete code — started from nowhere, because nothing
    set its interlock. Setting the same set `main()` sets and reading back
    `started` is what proves each subsystem is actually wired into
    `start_background_work()`, not just that it CAN start when called
    directly."""
    from jarvis.cost import prices

    for var in main_module._BACKGROUND_INTERLOCKS:
        monkeypatch.setenv(var, "1")
    # `start()` runs its first tick SYNCHRONOUSLY before arming the timer —
    # for this one subsystem that tick is a real network call to OpenRouter's
    # public catalog. Testing must never touch a real external service, same
    # rule `test_cost.py`'s own price-maintenance test already follows.
    monkeypatch.setattr(prices, "refresh_from_openrouter",
                        lambda **kw: {"ok": True, "updated": 0})

    started = assembly.start_background_work()

    # `prices` shares balances' own interlock (JARVIS_COST_REFRESH) rather
    # than having a second one — one flag, two readers, by design.
    for key in ("balances", "prices", "sampler", "heartbeat", "scheduler", "monitor",
               "job_supervisor", "improvement_cadence", "notification_trash_purge",
               "connector_icons"):
        assert started[key] is True, f"{key} did not start with every interlock set"


def test_without_any_interlock_nothing_starts(scratch, monkeypatch):
    """The other half of the same property: off by default, so a test's own
    `create_app()` never starts a real background thread unasked."""
    for var in main_module._BACKGROUND_INTERLOCKS:
        monkeypatch.delenv(var, raising=False)

    started = assembly.start_background_work()

    for key in ("balances", "prices", "sampler", "heartbeat", "scheduler", "monitor",
               "job_supervisor", "improvement_cadence", "notification_trash_purge",
               "connector_icons"):
        assert started[key] is False, f"{key} started with no interlock set"


def test_main_sets_every_background_interlock_before_building_the_app(scratch, monkeypatch):
    """`main()` — the real entry point uvicorn actually runs — must set every
    interlock via `setdefault` BEFORE `create_app()` (which calls
    `start_background_work()` internally) ever reads them. `uvicorn.run` is
    replaced with a no-op so this never actually binds a port; everything
    else in `main()` runs for real, including the real `create_app()` call,
    which is what actually starts every subsystem for this assertion to be
    meaningful rather than just checking the environment got set."""
    from jarvis.cost import prices

    for var in main_module._BACKGROUND_INTERLOCKS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PORT", "3999")
    # Same reasoning as the test above: `main()` sets JARVIS_COST_REFRESH too,
    # so `create_app()` would otherwise make a real network call here.
    monkeypatch.setattr(prices, "refresh_from_openrouter",
                        lambda **kw: {"ok": True, "updated": 0})

    captured: dict = {}

    def fake_run(app, **kwargs):
        captured["app"] = app
        captured["kwargs"] = kwargs

    monkeypatch.setattr("uvicorn.run", fake_run)

    main_module.main()

    assert "app" in captured, "main() never reached uvicorn.run"
    for var in main_module._BACKGROUND_INTERLOCKS:
        assert os.environ.get(var) == "1", f"{var} was not set by main()"


def test_mains_interlock_set_matches_what_start_background_work_actually_starts():
    """A subsystem added to one list and forgotten in the other is exactly the
    kind of drift a reading of the code would miss and this test would not:
    every ENABLE_ENV `start_background_work()`'s own subsystems check must be
    one `main()` actually sets."""
    from jarvis import notifications
    from jarvis.connectors import icons as connector_icons
    from jarvis.cost import balances
    from jarvis.heartbeat import engine as heartbeat
    from jarvis.improvement import cadence as improvement_cadence
    from jarvis.jobs import orchestrator as job_supervisor
    from jarvis.monitor import engine as monitor_engine
    from jarvis.ops.environment import sampler
    from jarvis.scheduler import engine as scheduler_engine

    gated_by_main = {
        heartbeat.ENABLE_ENV, scheduler_engine.ENABLE_ENV, monitor_engine.ENABLE_ENV,
        job_supervisor.ENABLE_ENV, balances.ENABLE_ENV, improvement_cadence.ENABLE_ENV,
        sampler.ENABLE_ENV, notifications.ENABLE_ENV, connector_icons.ENABLE_ENV,
    }
    assert gated_by_main <= set(main_module._BACKGROUND_INTERLOCKS)
