"""narrate_to_file: a script becomes a real audio file through the voice service
Jarvis already speaks with — tested against a real HTTP server speaking a real
provider's request shape (Fish Audio's, via `tts/generic.py`), not a mock of the
tool. The claims: the exact script is what gets sent, the audio that comes back
is what gets kept, and with no voice set up nothing pretends to have been made.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from jarvis import external_services, prefs
from jarvis.artifacts import store as artifact_store
from jarvis.capabilities import Risk
from jarvis.db import reset_for_tests as reset_db
from jarvis.tools import narrate_to_file
from jarvis.tts import generic

FAKE_MP3 = b"ID3\x03\x00\x00\x00fake-mp3-frames" * 20


class _VoiceServer(BaseHTTPRequestHandler):
    received: list[dict] = []
    status = 200

    def do_POST(self):  # noqa: N802 — the stdlib's name
        length = int(self.headers.get("Content-Length") or 0)
        _VoiceServer.received.append({"auth": self.headers.get("Authorization"),
                                      "body": json.loads(self.rfile.read(length))})
        self.send_response(_VoiceServer.status)
        self.send_header("Content-Type", "audio/mpeg" if _VoiceServer.status == 200 else "text/plain")
        body = FAKE_MP3 if _VoiceServer.status == 200 else b"quota exceeded"
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # quiet
        pass


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    yield
    reset_db()


@pytest.fixture
def voice_service(monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), _VoiceServer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _VoiceServer.received = []
    _VoiceServer.status = 200
    endpoint = f"http://127.0.0.1:{server.server_address[1]}/v1/tts"
    monkeypatch.setattr(generic, "KNOWN_PROVIDERS",
                        [{**generic.KNOWN_PROVIDERS[0], "endpoint": endpoint}])
    service = external_services.add_or_update(label="Fish Audio", key="fish-test-key")
    prefs.set_prefs({"ttsProvider": service["ref"]})
    yield
    server.shutdown()


def test_a_script_becomes_a_kept_audio_file(voice_service):
    result = narrate_to_file._run(script="Welcome to the cart. Fresh espresso, every morning.",
                                  filename="cart intro")
    assert result["ok"], result
    assert _VoiceServer.received[0]["body"] == {
        "text": "Welcome to the cart. Fresh espresso, every morning."}
    assert _VoiceServer.received[0]["auth"] == "Bearer fish-test-key"
    assert result["name"] == "cart intro.mp3" and result["mimeType"] == "audio/mpeg"
    kept = artifact_store.get(result["id"])
    assert kept.path.read_bytes() == FAKE_MP3
    assert result["ui_action"] == {"type": "attachment", "kind": "audio", "url": result["url"],
                                   "mimeType": "audio/mpeg", "name": "cart intro.mp3",
                                   "artifactId": result["id"], "title": "cart intro.mp3",
                                   "artifactKind": "audio", "size": len(FAKE_MP3)}


def test_with_no_voice_service_nothing_is_pretended():
    result = narrate_to_file._run(script="Hello there.")
    assert result["ok"] is False and "No voice service is set up" in result["error"]
    assert artifact_store.recent() == []


def test_the_services_own_refusal_is_reported(voice_service):
    _VoiceServer.status = 429
    result = narrate_to_file._run(script="Hello there.")
    assert result["ok"] is False and "quota exceeded" in result["error"]
    assert artifact_store.recent() == []


def test_an_overlong_script_is_refused_before_anything_is_sent(voice_service):
    result = narrate_to_file._run(script="x" * (narrate_to_file.MAX_CHARACTERS + 1))
    assert result["ok"] is False and "in sections" in result["error"]
    assert _VoiceServer.received == []


def test_it_asks_first_because_it_writes_a_file_and_may_cost_money():
    assert narrate_to_file.SPEC.risk is Risk.MEDIUM
    assert "Make a voice-over" in narrate_to_file.SPEC.summarize({"script": "Hi"})
