"""Recording the screen, sharing it, and watching it for a condition.

The ffmpeg process is replaced; everything deciding what to run, how to stop it
and what to hand back is real. Stopping in particular is worth a test of its
own: a killed ffmpeg leaves a file that exists, has a plausible size, and does
not play — a failure that looks exactly like success.
"""

from __future__ import annotations

import pytest

from fake_desktop import PNG_BYTES, FakeDesktop, element, window
from jarvis.control import captures, desktop, recorder, watching


@pytest.fixture(autouse=True)
def _isolated(scratch):
    desktop.reset_for_tests()
    recorder.reset_for_tests()
    watching.reset_for_tests()
    yield
    recorder.reset_for_tests()
    watching.reset_for_tests()
    desktop.reset_for_tests()


class FakeFfmpeg:
    """A process that writes its file when asked to stop, the way ffmpeg does."""

    def __init__(self, path, *, writes=True, ignores_q=False):
        self.path = path
        self.writes = writes
        self.ignores_q = ignores_q
        self.stdin = self
        self.killed = False
        self.told_to_quit = False
        self._done = False

    # stdin
    def write(self, data):
        if data == b"q":
            self.told_to_quit = True

    def flush(self):
        pass

    def close(self):
        if self.told_to_quit and not self.ignores_q and self.writes:
            self.path.write_bytes(b"\x00\x00\x00\x18ftypmp42fake video bytes")
            self._done = True

    def poll(self):
        return 0 if self._done else None

    def wait(self, timeout=None):
        if self.ignores_q:
            raise TimeoutError("still going")
        self._done = True
        return 0

    def kill(self):
        self.killed = True
        self._done = True


@pytest.fixture
def ffmpeg(monkeypatch):
    made = {}

    def spawn(args):
        made["args"] = args
        made["process"] = FakeFfmpeg(__import__("pathlib").Path(args[-1]))
        return made["process"]

    made["spawn"] = spawn
    return made


# --- recording ---------------------------------------------------------------

def test_recording_produces_a_file_a_browser_can_actually_play(ffmpeg, monkeypatch):
    """Raw desktop capture is not playable in a <video> element; this exact
    combination is, which is why the arguments are pinned rather than left to
    whatever ffmpeg defaults to."""
    monkeypatch.setattr(recorder, "find_ffmpeg", lambda: "ffmpeg")
    started = recorder.start(spawn=ffmpeg["spawn"])
    assert started["ok"] is True

    args = ffmpeg["args"]
    assert args[1:7] == ["-f", "gdigrab", "-framerate", "12", "-i", "desktop"]
    assert "libx264" in args and "yuv420p" in args and "+faststart" in args
    assert args[-1].endswith(".mp4")


def test_stopping_asks_ffmpeg_to_finish_rather_than_killing_it(ffmpeg, monkeypatch):
    monkeypatch.setattr(recorder, "find_ffmpeg", lambda: "ffmpeg")
    recorder.start(spawn=ffmpeg["spawn"])
    result = recorder.stop()

    process = ffmpeg["process"]
    assert process.told_to_quit is True, "the clean-stop convention, not a kill"
    assert process.killed is False
    assert result["ok"] is True and result["bytes"] > 0
    assert result["url"].startswith("/api/control/recordings/")


def test_an_ffmpeg_that_will_not_stop_is_ended_but_only_after_asking(monkeypatch):
    monkeypatch.setattr(recorder, "find_ffmpeg", lambda: "ffmpeg")
    made = {}

    def spawn(args):
        made["process"] = FakeFfmpeg(__import__("pathlib").Path(args[-1]), ignores_q=True)
        return made["process"]

    recorder.start(spawn=spawn)
    result = recorder.stop()
    assert made["process"].told_to_quit is True
    assert made["process"].killed is True
    # It left nothing usable, and that is reported rather than presented as a
    # video the user can watch.
    assert result["ok"] is False and "usable file" in result["error"]


def test_no_ffmpeg_is_explained_in_plain_language(monkeypatch):
    monkeypatch.setattr(recorder, "find_ffmpeg", lambda: None)
    result = recorder.start(spawn=lambda args: None)
    assert result["ok"] is False
    assert "ffmpeg.org" in result["error"] and "PATH" in result["error"]
    assert "Error" not in result["error"], "an explanation, not an error message"


def test_two_recordings_at_once_is_refused(ffmpeg, monkeypatch):
    monkeypatch.setattr(recorder, "find_ffmpeg", lambda: "ffmpeg")
    recorder.start(spawn=ffmpeg["spawn"])
    assert recorder.start(spawn=ffmpeg["spawn"])["ok"] is False
    recorder.stop()


def test_stopping_when_nothing_is_recording_says_so():
    assert recorder.stop() == {"ok": False,
                               "error": "Nothing is being recorded right now."}


def test_the_finished_video_is_delivered_into_the_chat(ffmpeg, monkeypatch):
    monkeypatch.setattr(recorder, "find_ffmpeg", lambda: "ffmpeg")
    from jarvis.tools.screen_recording import START, STOP

    # The tool calls the real recorder end to end; only the platform check and
    # the process launch are replaced, so what is under test is the delivery.
    monkeypatch.setattr(recorder.sys, "platform", "win32")
    monkeypatch.setattr(recorder, "_spawn", ffmpeg["spawn"])
    assert START.handler()["ok"] is True
    result = STOP.handler()
    assert result["ui_action"]["kind"] == "video"
    assert result["ui_action"]["mimeType"] == "video/mp4"
    assert result["ui_action"]["url"].startswith("/api/control/recordings/")


def test_a_recording_is_served_back_inline(ffmpeg, monkeypatch, scratch):
    from starlette.testclient import TestClient

    from jarvis.main import create_app

    monkeypatch.setattr(recorder, "find_ffmpeg", lambda: "ffmpeg")
    recorder.start(spawn=ffmpeg["spawn"])
    stopped = recorder.stop()

    client = TestClient(create_app())
    listed = client.get("/api/control/recordings").json()
    assert [c["id"] for c in listed["recordings"]] == [stopped["id"]]

    response = client.get(stopped["url"])
    assert response.status_code == 200
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["content-disposition"].startswith("inline")


# --- sharing -----------------------------------------------------------------

def test_turning_sharing_on_describes_nothing():
    """Nothing was asked yet. Turning it on arms the badge and adds a line to
    the prompt; it is not a request for a description."""
    from jarvis.prompt import system_instruction
    from jarvis.tools.screen_sharing import START, STOP

    assert "Screen sharing is on" not in system_instruction()
    result = START.handler()
    assert result["ok"] is True and "Don't describe" in result["note"]
    assert watching.is_sharing() is True
    assert "Screen sharing is on" in system_instruction()

    STOP.handler()
    assert watching.is_sharing() is False
    assert "Screen sharing is on" not in system_instruction()


def test_the_toggle_and_the_spoken_instruction_are_the_same_state():
    from starlette.testclient import TestClient

    from jarvis.main import create_app
    from jarvis.tools.screen_sharing import STOP

    client = TestClient(create_app())
    client.post("/api/observation/share/start")
    assert watching.is_sharing() is True

    STOP.handler()                                   # said out loud instead
    assert client.get("/api/observation/status").json()["sharing"] is False


def test_stopping_everything_clears_a_glance_and_sharing_together():
    from starlette.testclient import TestClient

    from jarvis.main import create_app

    client = TestClient(create_app())
    watching.start_sharing()
    watching.show_indicator("a glance")
    body = client.post("/api/observation/stop").json()
    assert body["watching"] is False and body["sharing"] is False


# --- watching the screen for a condition -------------------------------------

@pytest.fixture
def fake():
    fake = FakeDesktop(
        windows_list=[window("1", "Untitled - Notepad", "notepad", foreground=True)],
        elements={"1": [element(0, "Edit", "Document", "invoice paid")]},
        process_list=["chrome", "explorer", "notepad"],
    )
    desktop.set_desktop_for_tests(fake)
    return fake


def test_a_desktop_watch_is_refused_only_where_there_is_no_desktop():
    from jarvis.monitor.engine import UnsupportedCheck, validate

    with pytest.raises(UnsupportedCheck) as refused:
        validate({"kind": "window_appears", "target": "chrome"})
    assert "can't see or control a screen" in str(refused.value)


def test_a_desktop_watch_is_accepted_once_there_is_one(fake):
    from jarvis.monitor.engine import validate

    for kind in ("window_appears", "process_running", "screen_looks_like"):
        validate({"kind": kind, "target": "chrome"})


@pytest.mark.parametrize("kind,target,expected", [
    ("window_appears", "notepad", True),
    ("window_appears", "photoshop", False),
    ("window_gone", "photoshop", True),
    ("window_gone", "notepad", False),
    ("window_title_matches", "Untitled", True),
    ("window_title_matches", "Saved", False),
    ("process_running", "chrome", True),
    ("process_running", "photoshop", False),
    ("process_gone", "photoshop", True),
    ("element_text_matches", "invoice paid", True),
    ("element_text_matches", "invoice overdue", False),
])
def test_watching_windows_and_processes(fake, kind, target, expected):
    from jarvis.monitor.engine import evaluate

    triggered, _ = evaluate({"check": {"kind": kind, "target": target}})
    assert triggered is expected


def test_a_process_is_matched_the_way_people_name_it(fake):
    """People say "chrome", not "chrome.exe" — a watch that needs the exact
    executable name is a watch that never fires."""
    from jarvis.monitor.engine import evaluate

    fake.process_list = ["chrome.exe", "notepad.exe"]
    assert evaluate({"check": {"kind": "process_running", "target": "chrome"}})[0] is True


def test_the_one_condition_that_costs_a_model_call_asks_a_yes_no_question(fake, monkeypatch):
    from jarvis.ai import Reply
    from jarvis.monitor.engine import evaluate

    seen = {}

    def fake_ask(prompt, **kwargs):
        seen["prompt"] = prompt
        seen["json"] = kwargs.get("want_json")
        seen["lit"] = watching.state().watching
        return Reply(ok=True, data={"matches": True})

    monkeypatch.setattr("jarvis.ai.ask_model", fake_ask)
    triggered, _ = evaluate({"check": {"kind": "screen_looks_like",
                                       "description": "the export finished"}})
    assert triggered is True
    assert seen["json"] is True
    assert "the export finished" in seen["prompt"]
    # The badge is lit while it looks: a watch is a reason to see the screen,
    # not an exemption from saying so.
    assert seen["lit"] is True
    assert watching.state().watching is False


def test_a_screen_watch_that_cannot_see_does_not_fire(fake, monkeypatch):
    """No model, or an unreadable answer, is not a trigger — a watch that fires
    because it could not look is worse than one that keeps waiting."""
    from jarvis.ai import Reply
    from jarvis.monitor.engine import evaluate

    monkeypatch.setattr("jarvis.ai.ask_model",
                        lambda prompt, **kw: Reply(ok=False, error="no vision model"))
    assert evaluate({"check": {"kind": "screen_looks_like", "description": "x"}})[0] is False

    monkeypatch.setattr("jarvis.ai.ask_model",
                        lambda prompt, **kw: Reply(ok=True, data=None))
    assert evaluate({"check": {"kind": "screen_looks_like", "description": "x"}})[0] is False


def test_recording_is_refused_where_there_is_no_windows_desktop(monkeypatch):
    """`gdigrab` is the Windows desktop capture device. ffmpeg here is real; it
    has no screen to point at."""
    monkeypatch.setattr(recorder, "find_ffmpeg", lambda: "ffmpeg")
    result = recorder.start()
    assert result["ok"] is False and "isn't Windows" in result["error"]
