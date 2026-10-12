"""Jarvis's browser, against a real Chromium and a real (loopback) website.

Everything between the browser and the last socket is production code: the real
destination checks, the real egress proxy, a real Chrome or Edge. Only the final
connect is redirected to a fixture (see `browser_site.py`).
"""

from __future__ import annotations

import struct

import pytest
from browser_site import BASE, FixtureSite

from jarvis.browser import BrowserError
from jarvis.browser.locate import find_browser

needs_browser = pytest.mark.skipif(find_browser() is None, reason="no Chrome or Edge to drive")
pytestmark = needs_browser

SID = "0a7e5d11-6a72-4a76-8a1c-0000000000a1"
SID2 = "0a7e5d11-6a72-4a76-8a1c-0000000000a2"


@pytest.fixture
def site():
    fixture = FixtureSite()
    yield fixture
    fixture.close()


@pytest.fixture
def host(site, tmp_path):
    browser = site.host(tmp_path / "sessions")
    yield browser
    browser.shutdown()


def _by_label(elements, label):
    return next(e for e in elements if e["label"] == label)


def test_opening_a_page_reads_its_text_and_lists_what_can_be_clicked(host):
    host.open(SID, f"{BASE}/")
    page = host.read(SID, elements=True)
    assert page["title"] == "Fixture Home"
    assert "Hello from the fixture site" in page["text"]
    labels = {e["label"] for e in page["elements"]}
    assert {"Click me", "your name", "Second page", "Alert", "Popup"} <= labels
    assert all(element["selector"] for element in page["elements"])


def test_a_selector_from_the_list_clicks_the_real_element(host):
    host.open(SID, f"{BASE}/")
    page = host.read(SID, elements=True)
    clicked = host.click(SID, _by_label(page["elements"], "Click me")["selector"])
    assert "the button was clicked" in clicked["text"]


def test_typing_fills_a_field_and_submit_presses_enter(host):
    host.open(SID, f"{BASE}/")
    field = _by_label(host.read(SID, elements=True)["elements"], "your name")["selector"]
    typed = host.type_into(SID, field, "Ada", submit=True)
    assert "submitted: Ada" in typed["text"]
    # And the next read shows the field's value, so the model can confirm what it did.
    assert any(e.get("value") == "Ada" for e in typed["elements"])


def test_a_dropdown_is_chosen_by_its_label(host):
    host.open(SID, f"{BASE}/")
    select = next(e for e in host.read(SID, elements=True)["elements"] if e["tag"] == "select")
    assert select["options"] == ["Red", "Green"]
    host.type_into(SID, select["selector"], "Green")
    value = host.read(SID, elements=True)
    chosen = next(e for e in value["elements"] if e["tag"] == "select")
    assert chosen["selector"] == select["selector"]


def test_a_password_field_never_has_its_value_listed(host):
    host.open(SID, f"{BASE}/")
    elements = host.read(SID, elements=True)["elements"]
    password = next(e for e in elements if e.get("type") == "password")
    assert "value" not in password
    assert "hunter2" not in str(elements)


def test_a_link_that_wants_a_new_tab_opens_in_this_one(host):
    host.open(SID, f"{BASE}/")
    link = _by_label(host.read(SID, elements=True)["elements"], "Second page in a new tab")
    after = host.click(SID, link["selector"])
    assert after["url"] == f"{BASE}/two" and "This is page two." in after["text"]


def test_a_popup_is_dismissed_and_a_dialog_does_not_freeze_the_page(host):
    host.open(SID, f"{BASE}/")
    elements = host.read(SID, elements=True)["elements"]
    host.click(SID, _by_label(elements, "Popup")["selector"])
    assert host.read(SID)["url"] == f"{BASE}/", "a popup replaced the page"
    after = host.click(SID, _by_label(elements, "Alert")["selector"])
    assert "after alert" in after["text"], "an alert() froze the page"


def test_scrolling_reaches_what_is_below_the_fold(host):
    host.open(SID, f"{BASE}/tall")
    first = host.read(SID, elements=True)
    assert first["scroll"]["y"] == 0
    assert any(e["label"] == "Low button" for e in first["elements"]), \
        "an element far down should still be listed so it can be reached"
    after = host.scroll(SID, "bottom")
    assert after["scroll"]["y"] > 1000
    assert host.scroll(SID, "top")["scroll"]["y"] == 0


def test_a_page_longer_than_the_cap_is_cut_and_says_so(host):
    host.open(SID, f"{BASE}/long")
    page = host.read(SID)
    assert page["truncated"] is True and len(page["text"]) == 100_000


def test_a_screenshot_is_a_real_png_of_the_fixed_size(host):
    host.open(SID, f"{BASE}/")
    png = host.screenshot(SID)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", png[16:24])
    assert (width, height) == (1280, 800)


def test_the_person_can_click_and_type_by_coordinates(host):
    host.open(SID, f"{BASE}/")
    assert host.input(SID, {"type": "scroll", "deltaY": 200})["status"] == "active"
    host.input(SID, {"type": "key", "key": "Tab"})
    for bad in ({"type": "click", "x": -1, "y": 5}, {"type": "click", "x": 1280, "y": 5},
                {"type": "click", "x": float("nan"), "y": 5}, {"type": "key", "key": "F12"},
                {"type": "text", "text": "x" * 10_001}, {"type": "scroll", "deltaY": 9999},
                {"type": "teleport"}):
        with pytest.raises(BrowserError) as refused:
            host.input(SID, bad)
        assert refused.value.code == "INVALID_INPUT", bad


def test_a_redirect_to_a_private_address_is_refused_and_the_page_is_blanked(host):
    host.open(SID, f"{BASE}/")
    with pytest.raises(BrowserError) as refused:
        host.navigate(SID, f"{BASE}/to-private")
    assert refused.value.code == "BLOCKED_URL"
    assert host.read(SID)["url"] == "about:blank", "the private page was left showing"


@pytest.mark.parametrize("url", ["http://127.0.0.1:3000/api/health", "http://localhost/",
                                 "http://169.254.169.254/", "file:///C:/Windows/win.ini",
                                 f"{BASE}:8443/"])
def test_jarvis_own_api_and_the_local_network_are_not_reachable(host, url):
    with pytest.raises(BrowserError) as refused:
        host.open(SID, url)
    assert refused.value.code == "BLOCKED_URL"
    # A first launch that failed leaves nothing behind.
    assert host.sessions() == []


def test_a_subrequest_to_a_private_address_is_blocked_too(host, site):
    """A page that is itself public but loads something from the router."""
    def hook(handler, path):
        if path != "/spy":
            return False
        body = (b"<!doctype html><title>Spy</title><body><p id=r>pending</p><script>"
                b"fetch('http://127.0.0.1:%d/').then(()=>r.textContent='reached')"
                b".catch(()=>r.textContent='blocked')</script></body>" % site.port)
        handler._send(body)
        return True

    site.add_route(hook)
    host.open(SID, f"{BASE}/spy")
    import time

    deadline = time.time() + 8
    while time.time() < deadline and "pending" in host.read(SID)["text"]:
        time.sleep(0.2)
    assert "blocked" in host.read(SID)["text"]


def test_no_more_than_three_windows_at_once(host):
    ids = [f"0a7e5d11-6a72-4a76-8a1c-0000000000b{n}" for n in range(4)]
    for session_id in ids[:3]:
        host.open(session_id, f"{BASE}/")
    with pytest.raises(BrowserError) as refused:
        host.open(ids[3], f"{BASE}/")
    assert refused.value.code == "SESSION_LIMIT"
    host.close_session(ids[0])
    host.open(ids[3], f"{BASE}/")                       # a slot freed up


def test_a_closed_window_reopens_still_signed_in(host):
    host.open(SID, f"{BASE}/storage")
    assert "had:null" in host.read(SID)["text"]
    closed = host.close_session(SID)
    assert closed["status"] == "closed"
    with pytest.raises(BrowserError) as refused:
        host.read(SID)
    assert refused.value.code == "SESSION_CLOSED" and refused.value.status == 409
    host.open(SID, f"{BASE}/storage")
    assert "had:yes" in host.read(SID)["text"], "the profile did not survive closing"


def test_a_sign_in_made_of_a_session_cookie_survives_closing_and_reopening(host):
    """A login with no "remember me" is a session cookie, which a browser keeps only in memory.
    Found live against a real site: reopening a closed window lost it, because closing the old
    startup tab ended the browser's session and dropped the cookies that had just been restored."""
    host.open(SID, f"{BASE}/set-cookie")
    host.navigate(SID, f"{BASE}/cookie-echo")
    assert "sid=signed-in" in host.read(SID)["text"]
    host.close_session(SID)
    host.open(SID, f"{BASE}/cookie-echo")
    assert "sid=signed-in" in host.read(SID)["text"], "closing and reopening lost the sign-in"


def test_a_sign_in_is_written_down_while_the_window_is_open_so_a_hard_exit_keeps_it(
        site, host, tmp_path):
    """Nothing closes gracefully when the console window is shut. The cookies must already be
    on disk: copy what a killed Jarvis would have left (no close ever ran) into a fresh place."""
    import json
    import shutil

    host.open(SID, f"{BASE}/set-cookie")
    host.navigate(SID, f"{BASE}/two")
    saved = tmp_path / "sessions" / SID / "storage.json"
    assert saved.exists(), "nothing was written down while the window was open"
    assert any(c["name"] == "sid" for c in json.loads(saved.read_text())["cookies"])

    survivor = tmp_path / "after-a-kill"
    (survivor / SID).mkdir(parents=True)
    shutil.copy(saved, survivor / SID / "storage.json")
    shutil.copy(tmp_path / "sessions" / SID / "session.json", survivor / SID / "session.json")
    afterwards = site.host(survivor)
    try:
        afterwards.open(SID, f"{BASE}/cookie-echo")
        assert "sid=signed-in" in afterwards.read(SID)["text"]
    finally:
        afterwards.shutdown()


def test_a_window_whose_tab_died_is_replaced_by_the_next_open(host):
    """A tab can die under a window (a crash, a browser killed under memory pressure). The next
    open must give a working window, not collide with what is left of the old one."""
    host.open(SID, f"{BASE}/storage")

    async def kill_the_tab(manager):
        await manager._running[SID].page.close()

    host.run(kill_the_tab)
    assert host.is_running(SID) is False
    with pytest.raises(BrowserError) as dead:
        host.read(SID)
    assert dead.value.code == "SESSION_CLOSED"
    host.open(SID, f"{BASE}/two")
    assert host.read(SID)["title"] == "Second"
    assert host.is_running(SID) is True


def test_sessions_are_remembered_across_a_restart_of_the_host(site, tmp_path):
    first = site.host(tmp_path / "sessions")
    first.open(SID, f"{BASE}/two")
    first.shutdown()
    second = site.host(tmp_path / "sessions")
    try:
        remembered = second.session(SID)
        assert remembered["status"] == "closed" and remembered["title"] == "Second"
        assert second.sessions() == [remembered]
    finally:
        second.shutdown()


def test_a_window_left_idle_is_closed_and_can_be_reopened(site, tmp_path):
    host = site.host(tmp_path / "sessions", idle_timeout_s=0.1)
    try:
        host.open(SID, f"{BASE}/")
        manager = host._manager
        # The sweep runs every minute in life; drive one pass directly.
        import asyncio

        async def one_pass():
            for session_id, running in list(manager._running.items()):
                import time

                if time.monotonic() - running.touched > manager._idle_timeout_s:
                    await manager.close_session(session_id)

        import time

        time.sleep(0.3)
        host.run(lambda m: one_pass())
        assert host.session(SID)["status"] == "closed"
        host.open(SID, f"{BASE}/two")
        assert host.read(SID)["title"] == "Second"
    finally:
        host.shutdown()


def test_an_unknown_or_malformed_session_is_refused(host):
    with pytest.raises(BrowserError) as refused:
        host.session("not-a-uuid")
    assert refused.value.code == "INVALID_SESSION"
    with pytest.raises(BrowserError) as missing:
        host.session(SID2)
    assert missing.value.code == "SESSION_NOT_FOUND" and missing.value.status == 404


def test_acting_on_a_selector_that_is_not_there_says_what_to_do(host):
    host.open(SID, f"{BASE}/")
    with pytest.raises(BrowserError) as refused:
        host.click(SID, "#nope")
    assert refused.value.code == "ACTION_FAILED"
    assert "Read the page again" in refused.value.message
    with pytest.raises(BrowserError) as malformed:
        host.click(SID, "###")
    assert malformed.value.code == "ACTION_FAILED"


def test_the_browser_gets_no_secrets_from_jarvis_environment(host, monkeypatch):
    """Chromium is started with the scrubbed environment, so a key in Jarvis's own
    never reaches a process that renders arbitrary pages."""
    from jarvis.childenv import scrubbed_environment

    monkeypatch.setenv("GEMINI_API_KEY", "super-secret")
    assert "GEMINI_API_KEY" not in scrubbed_environment()
