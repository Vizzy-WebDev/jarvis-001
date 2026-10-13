"""Several wake phrases at once, one openWakeWord model per phrase (voice/wake.py).

The tests that need real models read them from `JARVIS_WAKE_TEST_MODELS` — a folder
holding `hey_jarvis_v0.1.onnx`, `melspectrogram.onnx` and `embedding_model.onnx` (copied
from a running install's `data/wakeword/`). The models are not in the repository: the
published ones carry a non-commercial licence. Without that folder those tests skip.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from jarvis import assembly
from jarvis.main import create_app
from jarvis.voice.wake import FRAME_SAMPLES, WakeDetector, slug

MODELS = os.environ.get("JARVIS_WAKE_TEST_MODELS", "")
needs_models = pytest.mark.skipif(not (MODELS and Path(MODELS, "hey_jarvis_v0.1.onnx").exists()),
                                  reason="JARVIS_WAKE_TEST_MODELS is not set to a folder of real models")


@pytest.fixture(autouse=True)
def _isolate(scratch, monkeypatch):
    monkeypatch.delenv("JARVIS_WAKE_MODEL_DOWNLOAD", raising=False)
    assembly.reset_for_tests()
    yield
    assembly.reset_for_tests()


def install(*names: str, as_: dict[str, str] | None = None) -> Path:
    """Copy real models into this test's data folder; `as_` renames (source -> target)."""
    target = WakeDetector.model_dir()
    target.mkdir(parents=True, exist_ok=True)
    for name in names:
        shutil.copy(Path(MODELS, name), target / name)
    for source, renamed in (as_ or {}).items():
        shutil.copy(Path(MODELS, source), target / renamed)
    return target


def test_a_phrase_finds_its_model_by_name():
    assert slug("Wake up Jarvis") == "wake_up_jarvis"
    assert slug("  Jarvis, wake up! ") == "jarvis_wake_up"
    detector = WakeDetector(phrases=["Hey Jarvis", "Hello Jarvis"])
    base = WakeDetector.model_dir()
    base.mkdir(parents=True, exist_ok=True)
    (base / "hey_jarvis_v0.1.onnx").write_bytes(b"x")
    assert detector.model_file("Hey Jarvis").name == "hey_jarvis_v0.1.onnx"
    assert detector.model_file("Hello Jarvis") is None
    (base / "hello_jarvis.onnx").write_bytes(b"x")
    assert detector.model_file("hello jarvis").name == "hello_jarvis.onnx"


def test_a_phrase_with_no_model_is_kept_and_said_to_need_one_never_claimed():
    detector = WakeDetector(phrases=["Hello Jarvis", "Computer"])
    detector.load()
    state = detector.status()
    assert state["available"] is False
    assert [p["state"] for p in state["phrases"]] == ["needs_model", "needs_model"]
    assert "none of your wake phrases has its model yet" in state["note"]


@needs_models
def test_every_listed_phrase_with_a_model_is_heard_together_in_one_model():
    install("hey_jarvis_v0.1.onnx", "melspectrogram.onnx", "embedding_model.onnx",
            as_={"hey_jarvis_v0.1.onnx": "wake_up_jarvis.onnx"})
    detector = WakeDetector(phrases=["Hey Jarvis", "Wake up Jarvis", "Jarvis wake up"])
    assert detector.load()
    assert set(detector._keys.values()) == {"Hey Jarvis", "Wake up Jarvis"}
    states = {p["phrase"]: p["state"] for p in detector.status()["phrases"]}
    assert states == {"Hey Jarvis": "ready", "Wake up Jarvis": "ready", "Jarvis wake up": "needs_model"}
    # Quiet audio wakes nothing, whichever phrase models are listening.
    for _ in range(25):
        assert detector.feed(np.zeros(FRAME_SAMPLES, dtype=np.int16).tobytes()) is None


@needs_models
def test_a_phrase_taken_off_the_list_is_no_longer_listened_for():
    install("hey_jarvis_v0.1.onnx", "melspectrogram.onnx", "embedding_model.onnx",
            as_={"hey_jarvis_v0.1.onnx": "hello_jarvis.onnx"})
    detector = WakeDetector(phrases=["Hey Jarvis", "Hello Jarvis"])
    assert detector.load()
    detector.set_phrases(["Hey Jarvis"])
    assert detector._preparing is not None
    detector._preparing.join(30)
    assert list(detector._keys.values()) == ["Hey Jarvis"]
    assert [p["phrase"] for p in detector.status()["phrases"]] == ["Hey Jarvis"]


@needs_models
def test_a_trained_model_can_be_added_and_is_listened_for_at_once():
    install("hey_jarvis_v0.1.onnx", "melspectrogram.onnx", "embedding_model.onnx")
    detector = WakeDetector(phrases=["Hey Jarvis", "Jarvis wake up"])
    assert detector.load()
    phrase = detector.add_model("jarvis_wake_up.onnx", Path(MODELS, "hey_jarvis_v0.1.onnx").read_bytes())
    assert phrase == "jarvis wake up"
    detector._preparing.join(30)
    states = {p["phrase"]: p["state"] for p in detector.status()["phrases"]}
    assert states == {"Hey Jarvis": "ready", "Jarvis wake up": "ready"}


@needs_models
def test_a_file_that_is_not_a_wake_phrase_model_is_refused_in_words():
    detector = WakeDetector()
    with pytest.raises(ValueError, match="not a model"):
        detector.add_model("hello_jarvis.onnx", b"not a model at all")
    with pytest.raises(ValueError, match="not an openWakeWord wake-phrase model"):
        detector.add_model("hello_jarvis.onnx", Path(MODELS, "melspectrogram.onnx").read_bytes())
    assert not (WakeDetector.model_dir() / "hello_jarvis.onnx").exists()


@needs_models
def test_a_model_put_in_place_after_a_failed_look_is_found_without_a_restart():
    detector = WakeDetector(phrases=["Hey Jarvis"])
    assert detector.load() is False                 # nothing on this computer yet
    install("hey_jarvis_v0.1.onnx", "melspectrogram.onnx", "embedding_model.onnx")
    detector.status()                               # anything asking looks again
    detector._preparing.join(30)
    assert detector.status()["phrases"] == [{"phrase": "Hey Jarvis", "state": "ready"}]


def test_the_route_refuses_what_is_not_an_onnx_file():
    client = TestClient(create_app())
    reply = client.post("/api/voice/wake/models?name=notes.txt", content=b"hello")
    assert reply.status_code == 400 and ".onnx" in reply.json()["error"]


def test_the_wake_socket_listens_for_the_saved_list(monkeypatch):
    from jarvis.prefs import set_prefs

    set_prefs({"wakePhrases": ["Hello Jarvis", "Computer"]})
    client = TestClient(create_app())
    with client.websocket_connect("/api/voice/wake") as socket:
        message = socket.receive_json()
    assert [p["phrase"] for p in message["phrases"]] == ["Hello Jarvis", "Computer"]
    assert message["type"] == "unavailable"     # no models in a scratch folder, said plainly


def test_with_nothing_saved_the_four_default_phrases_are_listened_for():
    body = TestClient(create_app()).get("/api/voice/status").json()
    assert [p["phrase"] for p in body["wake"]["phrases"]] == [
        "Hey Jarvis", "Hello Jarvis", "Wake up Jarvis", "Jarvis wake up"]
