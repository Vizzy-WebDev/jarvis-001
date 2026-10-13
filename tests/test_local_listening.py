"""Recognition on this computer: the model downloader and the `/api/duplex` socket.

Downloads are exercised against a real HTTP server on this machine serving a real
`.tar.bz2`, never the internet. The test that runs the real recogniser needs sherpa-onnx and
the models: `JARVIS_TEST_SPEECH_MODELS` names a folder holding `silero_vad.onnx` and
`moonshine-base-en/` (as `data/speech/` lays them out); without it that test skips.
"""

from __future__ import annotations

import base64
import http.server
import io
import os
import shutil
import tarfile
import threading
import time
import wave
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from jarvis import speech_models
from jarvis.main import create_app
from jarvis.prefs import set_prefs

MODELS = os.environ.get("JARVIS_TEST_SPEECH_MODELS", "")


@pytest.fixture(autouse=True)
def _isolate(scratch, monkeypatch):
    monkeypatch.delenv(speech_models.DOWNLOAD_ENV, raising=False)
    speech_models.reset_for_tests()
    yield
    speech_models.reset_for_tests()


# --- the downloader ---------------------------------------------------------------

def serve(payload: bytes):
    """A real HTTP server on this machine answering every GET with `payload`."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            for i in range(0, len(payload), 4096):
                self.wfile.write(payload[i:i + 4096])

        def log_message(self, *_):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}/model.tar.bz2"


def archive(top: str, files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:bz2") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_nothing_is_downloaded_without_the_interlock():
    state = speech_models.ensure("moonshine-tiny-en")
    assert state["state"] == "failed" and "not allowed to download" in state["error"]
    assert not speech_models.folder("moonshine-tiny-en").exists()


def test_a_model_is_fetched_with_real_progress_and_unpacked_into_place(monkeypatch):
    monkeypatch.setenv(speech_models.DOWNLOAD_ENV, "1")
    payload = archive("sherpa-onnx-moonshine-tiny", {
        "encoder_model.ort": b"e" * 200_000, "decoder_model_merged.ort": b"d", "tokens.txt": b"t"})
    server, url = serve(payload)
    model = speech_models.CATALOG["moonshine-tiny-en"]
    monkeypatch.setitem(speech_models.CATALOG, "moonshine-tiny-en",
                        speech_models.SpeechModel(model.id, model.label, url, len(payload), model.needs))
    try:
        speech_models.ensure("moonshine-tiny-en")
        deadline = time.monotonic() + 30
        while speech_models.status("moonshine-tiny-en")["state"] != "ready":
            assert time.monotonic() < deadline, speech_models.status("moonshine-tiny-en")
            time.sleep(0.05)
    finally:
        server.shutdown()
    base = speech_models.folder("moonshine-tiny-en")
    assert sorted(p.name for p in base.iterdir()) == ["decoder_model_merged.ort", "encoder_model.ort", "tokens.txt"]
    assert speech_models.status("moonshine-tiny-en")["done"] == len(payload)
    assert not list(speech_models.root().glob("*.part"))


def test_a_download_missing_a_file_is_never_mistaken_for_a_model(monkeypatch):
    monkeypatch.setenv(speech_models.DOWNLOAD_ENV, "1")
    payload = archive("broken", {"encoder_model.ort": b"e"})
    server, url = serve(payload)
    model = speech_models.CATALOG["moonshine-tiny-en"]
    monkeypatch.setitem(speech_models.CATALOG, "moonshine-tiny-en",
                        speech_models.SpeechModel(model.id, model.label, url, len(payload), model.needs))
    try:
        speech_models.ensure("moonshine-tiny-en")
        deadline = time.monotonic() + 30
        while speech_models.status("moonshine-tiny-en")["state"] not in ("failed", "ready"):
            assert time.monotonic() < deadline
            time.sleep(0.05)
    finally:
        server.shutdown()
    state = speech_models.status("moonshine-tiny-en")
    assert state["state"] == "failed" and "missing" in state["error"]
    assert not speech_models.folder("moonshine-tiny-en").exists()


# --- the socket -------------------------------------------------------------------

def first_message(client: TestClient) -> dict:
    with client.websocket_connect("/api/duplex") as socket:
        return socket.receive_json()


def test_a_cloud_service_chosen_with_no_key_is_said_plainly_never_swapped():
    set_prefs({"listeningProvider": "deepgram"})
    message = first_message(TestClient(create_app()))
    assert message["type"] == "error" and message["fatal"] is True
    assert "Deepgram" in message["error"] and "Settings" in message["error"]


def test_on_device_recognition_not_installed_says_how_to_fix_it(monkeypatch):
    from jarvis.stt import local

    monkeypatch.setattr(local, "available", lambda: False)
    message = first_message(TestClient(create_app()))
    assert message["type"] == "error" and message["fatal"] is True
    assert "Restart Jarvis" in message["error"]


def test_with_no_download_allowed_the_missing_model_is_reported(monkeypatch):
    from jarvis.stt import local

    monkeypatch.setattr(local, "available", lambda: True)
    message = first_message(TestClient(create_app()))
    assert message["type"] == "error" and message["fatal"] is True
    assert "not allowed to download" in message["error"]


@pytest.mark.skipif(not (MODELS and Path(MODELS, "moonshine-base-en").exists()),
                    reason="JARVIS_TEST_SPEECH_MODELS is not set to a folder of real models")
def test_spoken_audio_comes_back_as_words_from_this_computer():
    pytest.importorskip("sherpa_onnx")
    target = speech_models.root()
    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(Path(MODELS, "moonshine-base-en"), target / "moonshine-base-en")
    (target / "silero-vad").mkdir()
    shutil.copy(Path(MODELS, "silero_vad.onnx"), target / "silero-vad" / "silero_vad.onnx")
    speech = Path(MODELS, "sample.wav")           # someone saying a short sentence, 16 kHz mono
    with wave.open(str(speech)) as w:
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    audio = np.concatenate([audio, np.zeros(16000 * 2, dtype=np.int16)])

    client = TestClient(create_app())
    seen: list[dict] = []
    with client.websocket_connect("/api/duplex") as socket:
        message = socket.receive_json()
        while message["type"] in ("downloading", "loading"):
            message = socket.receive_json()
        assert message == {"type": "ready", "mode": "local", "model": "moonshine-base-en"}
        for i in range(0, len(audio), 1600):
            socket.send_json({"type": "audio", "data": base64.b64encode(audio[i:i + 1600].tobytes()).decode()})
        while not any(m["type"] == "utterance_end" for m in seen):
            seen.append(socket.receive_json())
        socket.send_json({"type": "end"})
    kinds = [m["type"] for m in seen]
    assert kinds[0] == "speech_started"
    words = " ".join(m["text"] for m in seen if m["type"] == "transcript")
    assert words.strip(), seen
    assert kinds.index("utterance_end") > max(i for i, k in enumerate(kinds) if k == "transcript")
