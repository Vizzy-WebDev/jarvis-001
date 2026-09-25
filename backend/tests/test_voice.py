"""Wake word (§15) and conversation mode (§16).

**What these tests can and cannot prove.** They exercise the real model on real
audio for the negative case — silence and noise must never wake it — and they
exercise every piece of plumbing around detection with the score forced, which is
the part that can actually be wrong in code. They cannot prove the model
recognises a genuine "hey Jarvis": that needs a microphone and a person, and
pretending a synthetic waveform stands in for one would be exactly the kind of
verification theatre this build avoids. That check belongs to the front end, with
a real voice, and is called out as still owed.

The conversation-mode tests run against a fake clock: every decision it makes is
about WHEN, and testing "when" against a real clock makes a test slow and flaky
at the same time.
"""

from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from jarvis import assembly
from jarvis.events import EventType
from jarvis.events.bus import EventBus
from jarvis.main import create_app
from jarvis.voice import ConversationMode, Mode, WakeDetector
from jarvis.voice.wake import FRAME_BYTES, FRAME_SAMPLES


@pytest.fixture(autouse=True)
def _isolate(scratch):
    assembly.reset_for_tests()
    yield
    assembly.reset_for_tests()


def pcm(samples: int, amplitude: int = 0, seed: int = 0) -> bytes:
    if amplitude == 0:
        return np.zeros(samples, dtype=np.int16).tobytes()
    rng = np.random.default_rng(seed)
    return rng.normal(0, amplitude, samples).astype(np.int16).tobytes()


# --- the real model, on the case it must get right ---------------------------

def test_silence_and_noise_never_wake_it():
    """The failure that matters most: waking on a room, not on a person."""
    detector = WakeDetector()
    if not detector.available:
        pytest.skip("the wake model is not downloaded in this environment")
    for _ in range(20):
        assert detector.feed(pcm(FRAME_SAMPLES)) is None
    for i in range(20):
        assert detector.feed(pcm(FRAME_SAMPLES, amplitude=3000, seed=i)) is None


def test_an_unavailable_model_is_reported_not_faked(monkeypatch):
    """Degrading into "wakes on any noise" would be far worse than not working."""
    detector = WakeDetector()
    monkeypatch.setattr(detector, "_error", "ImportError: no such package")
    assert detector.available is False
    assert detector.feed(pcm(FRAME_SAMPLES * 4)) is None
    assert "not available" in detector.status()["note"]


# --- the plumbing around detection, with the score forced --------------------

class AlwaysWakes(WakeDetector):
    def load(self) -> bool:
        return True

    def _score(self, frame: bytes) -> float:
        return 0.99


def test_audio_is_buffered_into_exact_frames_not_padded():
    """A padded frame is silence the model never heard, and silently changes
    what it scores."""
    scored: list[int] = []

    class Counting(AlwaysWakes):
        def _score(self, frame: bytes) -> float:
            scored.append(len(frame))
            return 0.0

    detector = Counting()
    detector.feed(pcm(FRAME_SAMPLES // 2))          # half a frame: nothing yet
    assert scored == []
    detector.feed(pcm(FRAME_SAMPLES // 2))          # completes it
    assert scored == [FRAME_BYTES]


def test_one_phrase_wakes_once_not_three_times():
    """Without a refractory window a single "hey Jarvis" fires repeatedly as the
    phrase moves through the model's window, and the assistant appears to wake
    up three times to one greeting."""
    detector = AlwaysWakes(refractory_s=2.0)
    assert detector.feed(pcm(FRAME_SAMPLES), now=100.0) is not None
    assert detector.feed(pcm(FRAME_SAMPLES), now=100.5) is None
    assert detector.feed(pcm(FRAME_SAMPLES), now=101.9) is None
    assert detector.feed(pcm(FRAME_SAMPLES), now=102.1) is not None


def test_a_reset_forgets_the_previous_session():
    detector = AlwaysWakes()
    detector.feed(pcm(FRAME_SAMPLES), now=100.0)
    detector.reset()
    assert detector.feed(pcm(FRAME_SAMPLES), now=100.1) is not None


# --- conversation mode, on a fake clock --------------------------------------

def test_a_follow_up_needs_no_wake_word_until_the_room_goes_quiet():
    mode = ConversationMode(idle_timeout_s=25)
    mode.wake(now=0)
    assert mode.is_open

    mode.touch(now=20)               # they said something else
    assert mode.tick(now=40) is None, "the window runs from the last thing that happened"
    closed = mode.tick(now=46)
    assert closed is not None and mode.mode is Mode.ASLEEP
    assert "no one said anything" in closed.reason


def test_a_long_reply_does_not_close_the_window_while_it_is_still_playing():
    """The assistant speaking is activity too, or a slow answer closes the
    session while the user is still listening to it."""
    mode = ConversationMode(idle_timeout_s=10)
    mode.wake(now=0)
    for t in range(1, 30):
        mode.touch(now=float(t))     # chunks of a long reply
        assert mode.tick(now=float(t)) is None
    assert mode.is_open


def test_a_session_cannot_be_held_open_forever_by_background_noise():
    mode = ConversationMode(idle_timeout_s=10, max_session_s=60)
    mode.wake(now=0)
    for t in range(1, 80):
        mode.touch(now=float(t))
    closed = mode.tick(now=80)
    assert closed is not None and "too long" in closed.reason


def test_ticking_while_asleep_does_nothing():
    mode = ConversationMode()
    assert mode.tick(now=1000) is None
    assert mode.seconds_left(now=1000) == 0.0


def test_closing_early_is_honoured():
    mode = ConversationMode()
    mode.wake(now=0)
    mode.close(now=1, reason="the user ended it")
    assert not mode.is_open


# --- the route ---------------------------------------------------------------

def test_the_status_route_says_where_the_audio_goes():
    client = TestClient(create_app())
    body = client.get("/api/voice/status").json()
    assert body["mode"] == "asleep"
    assert "no audio leaves it" in body["wake"]["note"] or body["wake"]["available"] is False


def test_the_socket_announces_a_wake_and_opens_the_session(monkeypatch):
    monkeypatch.setattr(assembly, "_wake", AlwaysWakes())
    seen: list[str] = []
    from jarvis.events import bus

    unsubscribe = bus.subscribe(EventType.VOICE_WAKE, lambda e: seen.append(e.type.value))
    try:
        client = TestClient(create_app())
        with client.websocket_connect("/api/voice/wake") as socket:
            assert socket.receive_json()["type"] == "ready"
            socket.send_bytes(pcm(FRAME_SAMPLES))
            message = socket.receive_json()
            assert message["type"] == "wake" and message["score"] > 0.5
    finally:
        unsubscribe()

    assert seen == ["voice.wake"]
    assert assembly.get_conversation_mode().is_open


def test_the_socket_refuses_rather_than_swallowing_a_microphone(monkeypatch):
    """A socket that accepts audio it will never score is worse than a refusal."""
    detector = WakeDetector()
    monkeypatch.setattr(detector, "_error", "ImportError: no such package")
    monkeypatch.setattr(assembly, "_wake", detector)

    client = TestClient(create_app())
    with client.websocket_connect("/api/voice/wake") as socket:
        assert socket.receive_json()["type"] == "unavailable"
