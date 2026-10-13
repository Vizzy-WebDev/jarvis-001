"""Home's health cards (`GET /api/health`) and the honesty of what they report.

The cards are read every few seconds by an open Home, so the route is a read of
what other parts already know — never a probe — and every number it cannot know
is None rather than a zero that reads as "idle", "instant" or "free".
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from jarvis import assembly
from jarvis.main import create_app
from jarvis.voice import WakeDetector
from jarvis.voice.wake import DOWNLOAD_ENV


def _client() -> TestClient:
    return TestClient(create_app())


def test_the_route_answers_with_both_cards(scratch):
    body = _client().get("/api/health").json()
    system, jarvis = body["system"], body["jarvis"]
    for key in ("cpuPct", "memPct", "diskPct", "netBytesPerSec"):
        assert key in system
    assert 0 <= system["memPct"] <= 100
    assert 0 <= system["diskPct"] <= 100
    assert jarvis["uptimeSec"] >= 0
    assert jarvis["jobsRunning"] == 0
    assert jarvis["connectors"]["problems"] == []


def test_a_day_with_no_model_calls_says_so_rather_than_zero(scratch):
    """No calls today is not "free" and not "instant"."""
    jarvis = _client().get("/api/health").json()["jarvis"]
    assert jarvis["responseSec"] is None
    assert jarvis["spendToday"] is None


def test_no_model_ready_is_an_issue_in_plain_words(scratch):
    jarvis = _client().get("/api/health").json()["jarvis"]
    assert jarvis["modelReady"] is False
    assert "No model is ready to answer" in jarvis["issues"]


def test_network_throughput_is_unknown_until_there_is_an_interval(scratch):
    from jarvis.routes import health

    health._net_last = None  # noqa: SLF001
    assert health._network_bytes_per_second() is None  # noqa: SLF001
    time.sleep(0.05)
    second = health._network_bytes_per_second()  # noqa: SLF001
    assert second is None or second >= 0


# --- the wake word: reported as it is, fetched only when the launch allows it ---

def test_a_missing_wake_model_is_not_downloaded_without_the_interlock(scratch, monkeypatch):
    """A test (or anything not launched by main()) never reaches the network."""
    monkeypatch.delenv(DOWNLOAD_ENV, raising=False)
    called: list[object] = []
    import openwakeword.utils

    monkeypatch.setattr(openwakeword.utils, "download_models",
                        lambda *a, **k: called.append(a))
    detector = WakeDetector()
    assert detector.available is False
    assert called == []
    assert "not available" in detector.status()["note"]


def test_with_the_interlock_the_model_is_fetched_into_the_data_folder(scratch, monkeypatch):
    monkeypatch.setenv(DOWNLOAD_ENV, "1")
    asked: list[str] = []
    import openwakeword.utils

    def fake_download(model_names, target_directory):
        asked.append(target_directory)
        raise OSError("no network in this test")

    monkeypatch.setattr(openwakeword.utils, "download_models", fake_download)
    detector = WakeDetector()
    assert detector.available is False
    assert asked == [str(WakeDetector.model_dir())]
    assert "wakeword" in asked[0]


def test_status_never_waits_and_says_when_it_is_still_getting_ready(scratch, monkeypatch):
    import threading

    gate = threading.Event()

    class Slow(WakeDetector):
        def load(self) -> bool:
            gate.wait(5)
            return False

    detector = Slow()
    started = time.monotonic()
    state = detector.status()
    assert time.monotonic() - started < 1
    assert state["available"] is False and state["preparing"] is True
    assert "getting ready" in state["note"]
    gate.set()


def test_the_self_check_reports_the_wake_word_as_it_really_is(scratch, monkeypatch):
    """It used to say "available" unconditionally while no model was installed."""
    from jarvis.ops.environment import reachability

    detector = WakeDetector()
    monkeypatch.setattr(detector, "_error", "FileNotFoundError: not here")
    monkeypatch.setattr(assembly, "_wake", detector)
    assert reachability.voice()["wakeWord"]["available"] is False
