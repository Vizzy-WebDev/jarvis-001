"""Seeing the screen: window targeting, captures, and getting a picture into chat.

Everything above the last inch is the real implementation — the real tools, the
real capture store, the real route. `FakeDesktop` replaces only the part that
would touch a real mouse.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from fake_desktop import PNG_BYTES, FakeDesktop, element, window
from jarvis.control import captures, desktop, watching
from jarvis.control.safety import get_safety_config, set_safety_config


@pytest.fixture(autouse=True)
def _isolated(scratch):
    desktop.reset_for_tests()
    watching.reset_for_tests()
    yield
    desktop.reset_for_tests()
    watching.reset_for_tests()


@pytest.fixture
def client():
    from jarvis.main import create_app

    return TestClient(create_app())


@pytest.fixture
def fake():
    fake = FakeDesktop(windows_list=[
        window("1", "Untitled - Notepad", "notepad", foreground=True),
        window("2", "Inbox - Outlook", "outlook"),
    ])
    desktop.set_desktop_for_tests(fake)
    return fake


# --- what this machine actually is -------------------------------------------

def test_no_desktop_here_is_reported_rather_than_half_working():
    """This container has no screen, and that is the honest answer — not a
    silent no-op that lets a session 'succeed' having done nothing."""
    assert desktop.backend() == "none"
    assert desktop.available() is False
    text = desktop.describe_desktop()
    assert "can't see or control a screen" in text
    assert "not Windows" in text

    with pytest.raises(desktop.NoDesktopHere):
        desktop.get_desktop().windows()


def test_a_screen_tool_refuses_plainly_with_no_desktop():
    from jarvis.tools.look_at_screen import SPEC as look
    from jarvis.tools.take_screenshot import SPEC as shot

    for spec in (look, shot):
        result = spec.handler()
        assert result["ok"] is False
        assert "screen" in result["error"].lower()


# --- which window ------------------------------------------------------------

def test_jarvis_own_tab_is_never_what_it_looks_at():
    """The user is talking to Jarvis through a browser tab, so at the moment
    'look at my screen' runs, the front window is very often Jarvis itself."""
    windows = [window("1", "Jarvis", "chrome", foreground=True),
               window("2", "Untitled - Notepad", "notepad")]
    assert desktop.pick_window(windows).handle == "2"
    assert desktop.pick_window(windows, "chrome").handle == "2"


def test_a_window_merely_named_jarvis_is_not_mistaken_for_ours():
    """Matched on a browser process AND the real page title — a File Explorer
    window called "Jarvis-001" is a different thing entirely."""
    explorer = window("1", "Jarvis-001", "explorer", foreground=True)
    assert desktop.is_jarvis_own_window(explorer) is False
    assert desktop.pick_window([explorer]).handle == "1"


def test_looking_at_ourselves_beats_looking_at_nothing():
    only = [window("1", "Jarvis - chat", "chrome", foreground=True)]
    assert desktop.pick_window(only).handle == "1"


@pytest.mark.parametrize("target,expected", [
    ("", "1"), ("screen", "1"), ("whole screen", "1"),
    ("outlook", "2"), ("Inbox", "2"), ("notepad", "1"),
])
def test_targeting_by_name_or_title(target, expected):
    windows = [window("1", "Untitled - Notepad", "notepad", foreground=True),
               window("2", "Inbox - Outlook", "outlook")]
    assert desktop.pick_window(windows, target).handle == expected


# --- captures ----------------------------------------------------------------

def test_a_capture_lands_in_the_scratch_data_dir_not_the_real_one(scratch):
    """The regression finding 1 exists for: both originals built their path from
    the source file's own location, so an isolated test run wrote screenshots
    into the user's real data folder."""
    saved = captures.save(captures.SCREENSHOT, PNG_BYTES)
    assert saved.path.parent == scratch.data_dir / "screenshots"
    assert saved.path.read_bytes() == PNG_BYTES
    assert saved.as_dict()["url"] == f"/api/control/screenshots/{saved.id}"


def test_an_id_is_ours_and_a_path_cannot_be_smuggled_through_one():
    captures.save(captures.SCREENSHOT, PNG_BYTES)
    for bad in ("../../.env", "..", "a/b", "shot", "", "20240101-000000-zzzzzz"):
        assert captures.get("screenshot", bad) is None


def test_old_captures_are_pruned_by_count():
    set_safety_config({"screenshotRetention": {"maxCount": 3, "maxAgeHours": 24}})
    for _ in range(5):
        captures.save(captures.SCREENSHOT, PNG_BYTES)
    kept = captures.recent(captures.SCREENSHOT)
    # Exactly the limit, not one over: pruning makes room for the file about to
    # be written rather than pruning to the limit and then adding to it.
    assert len(kept) == 3


def test_captures_are_pruned_by_age():
    import os
    import time

    set_safety_config({"screenshotRetention": {"maxCount": 50, "maxAgeHours": 1}})
    stale = captures.save(captures.SCREENSHOT, PNG_BYTES)
    old = time.time() - 7200
    os.utime(stale.path, (old, old))
    captures.prune(captures.SCREENSHOT)
    assert captures.get("screenshot", stale.id) is None


def test_removing_a_default_block_pattern_makes_it_stay_removed():
    """A list is replaced, not merged — otherwise a default a user deliberately
    deleted reappears on the next read."""
    set_safety_config({"blockedWindowPatterns": ["only-this"]})
    assert get_safety_config()["blockedWindowPatterns"] == ["only-this"]
    # …while a nested retention block still merges over the defaults.
    set_safety_config({"screenshotRetention": {"maxCount": 5}})
    assert get_safety_config()["screenshotRetention"] == {"maxCount": 5, "maxAgeHours": 24}


# --- the tools, driven for real ----------------------------------------------

def test_taking_a_screenshot_returns_something_the_chat_can_show(fake):
    from jarvis.tools.take_screenshot import SPEC

    result = SPEC.handler()
    assert result["ok"] is True
    action = result["ui_action"]
    assert action == {"type": "attachment", "kind": "image",
                      "url": action["url"], "mimeType": "image/png"}
    assert action["url"].startswith("/api/control/screenshots/")
    assert fake.names() == ["windows", "screenshot"], "looking must not touch input"


def test_looking_at_the_screen_asks_a_model_and_answers_in_words(fake, monkeypatch):
    fake.elements["1"] = [element(0, "Edit", "Document", "the quick brown fox")]
    seen = {}

    def fake_ask(prompt, **kwargs):
        seen["prompt"] = prompt
        seen["need"] = kwargs.get("need")
        seen["media"] = kwargs.get("media")
        from jarvis.ai import Reply

        return Reply(ok=True, text="It's a Notepad window with a sentence in it.")

    monkeypatch.setattr("jarvis.ai.ask_model", fake_ask)
    from jarvis.tools.look_at_screen import SPEC

    result = SPEC.handler(question="what does it say?")
    assert result == {"ok": True, "answer": "It's a Notepad window with a sentence in it.",
                      "sawImage": True}
    assert seen["need"] == {"vision": True}
    assert seen["media"][0]["mimeType"] == "image/png"
    # The window's own text goes with the picture: real labels and values beat a
    # guess from pixels, and they are what lets a text-only model answer at all.
    assert "the quick brown fox" in seen["prompt"]
    assert "what does it say?" in seen["prompt"]


def test_a_text_only_model_still_answers_from_the_windows_own_text(fake, monkeypatch):
    fake.elements["1"] = [element(0, "Edit", "Document", "invoice total 42")]
    calls = []

    def fake_ask(prompt, **kwargs):
        from jarvis.ai import Reply

        calls.append(kwargs.get("need"))
        if kwargs.get("need"):                      # the vision attempt
            return Reply(ok=False, error="No model here can see an image.")
        return Reply(ok=True, text="It says the invoice total is 42.")

    monkeypatch.setattr("jarvis.ai.ask_model", fake_ask)
    from jarvis.tools.look_at_screen import SPEC

    result = SPEC.handler(question="what's the total?")
    assert result["ok"] is True and result["sawImage"] is False
    assert calls == [{"vision": True}, None]


def test_with_no_window_text_a_failed_vision_call_fails_honestly(fake, monkeypatch):
    def fake_ask(prompt, **kwargs):
        from jarvis.ai import Reply

        return Reply(ok=False, error="No model here can see an image.")

    monkeypatch.setattr("jarvis.ai.ask_model", fake_ask)
    from jarvis.tools.look_at_screen import SPEC

    result = SPEC.handler()
    assert result["ok"] is False
    assert "see an image" in result["error"]


def test_the_badge_is_lit_while_looking_and_released_afterwards(fake, monkeypatch):
    lit = []

    def fake_ask(prompt, **kwargs):
        from jarvis.ai import Reply

        lit.append(watching.state().watching)
        return Reply(ok=True, text="ok")

    monkeypatch.setattr("jarvis.ai.ask_model", fake_ask)
    from jarvis.tools.look_at_screen import SPEC

    SPEC.handler()
    # Lit for the whole span, including while the model is answering — not just
    # for the instant the pixels were grabbed.
    assert lit == [True]
    assert watching.state().watching is False


def test_the_badge_is_released_even_when_the_capture_fails(fake):
    fake.fail_on["screenshot"] = RuntimeError("the display went away")
    from jarvis.tools.take_screenshot import SPEC

    result = SPEC.handler()
    assert result["ok"] is False
    assert watching.state().watching is False


def test_sharing_and_a_glance_are_different_things():
    token = watching.show_indicator("a glance")
    assert watching.state().watching is True and watching.is_sharing() is False
    watching.hide_indicator(token)
    assert watching.state().watching is False

    watching.start_sharing()
    assert watching.state().watching is True and watching.is_sharing() is True
    # A glance during sharing does not end sharing when it finishes.
    token = watching.show_indicator("a glance")
    watching.hide_indicator(token)
    assert watching.is_sharing() is True
    watching.stop_everything()
    assert watching.state().as_dict() == {"watching": False, "sharing": False, "reasons": []}


# --- serving it back ---------------------------------------------------------

def test_a_capture_is_served_inline_and_a_missing_one_is_a_clean_404(client):
    saved = captures.save(captures.SCREENSHOT, PNG_BYTES)

    response = client.get(f"/api/control/screenshots/{saved.id}")
    assert response.status_code == 200
    assert response.content == PNG_BYTES
    assert response.headers["content-type"] == "image/png"
    # Inline, unlike an artifact: these bytes came from the OS, not from
    # anything a model wrote. The defence-in-depth headers stay.
    assert response.headers["content-disposition"].startswith("inline")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]

    assert client.get("/api/control/screenshots/20200101-000000-abcdef").status_code == 404
    assert client.get("/api/control/screenshots/20200101-000000-abcdef").json() == {
        "ok": False, "error": "Not found."}, "the recorded 404 shape, exactly"


def test_a_traversal_through_the_capture_route_does_not_reach_a_file(client, scratch):
    (scratch.data_dir / "secret.png").write_bytes(b"not yours")
    for attempt in ("../secret", "..%2Fsecret", "....//secret"):
        assert client.get(f"/api/control/screenshots/{attempt}").status_code in (404, 400)
