"""Jarvis speaking first (a finished background job, the heartbeat) appears in an open page.

`speak_now` saves the message to the active conversation and announces it on the event bus. The
page ignored the announcement, so the message showed up only after a reload — or folded into the
reply to whatever the person said next. Checked in a real browser against a real server.
"""

from __future__ import annotations

import time

import httpx
import pytest

from test_shell_e2e import CHROME, EXPORT, page, visit  # noqa: F401

pytestmark = [
    pytest.mark.skipif(not EXPORT.is_file(), reason="the front end has not been built"),
    pytest.mark.skipif(CHROME is None, reason="no Chromium in this environment"),
]


def test_a_message_jarvis_says_first_appears_without_a_reload(live_server, page):
    from jarvis.heartbeat.speak import speak_now
    from jarvis.session import get_active_session_id

    session_id = get_active_session_id()        # the conversation the page opens on
    visit(page, live_server)
    page.wait_for_timeout(800)                  # the page's event stream is connected

    speak_now('Boss, "Flights" is done. Three options.', reason="A background job finished.")

    page.get_by_text('"Flights" is done. Three options.').first.wait_for(timeout=10_000)
    stored = httpx.get(f"{live_server}/api/conversations/{session_id}", timeout=30).json()
    texts = [m.get("text") for m in stored.get("messages", [])]
    assert 'Boss, "Flights" is done. Three options.' in texts, "it was shown but never saved"
    assert page.get_by_text('"Flights" is done. Three options.').count() == 1


def test_a_message_for_another_conversation_is_not_shown_here(live_server, page):
    from jarvis.events import EventType, bus

    visit(page, live_server)
    page.wait_for_timeout(800)
    bus.publish(EventType.ASSISTANT_RESPONSE, {
        "sessionId": "some-other-chat", "proactive": True, "text": "Not for this chat."})
    time.sleep(1.5)

    assert page.get_by_text("Not for this chat.").count() == 0
