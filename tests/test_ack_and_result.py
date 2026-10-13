"""An acknowledgement ("on it") and the result that arrives later are two assistant messages, in order,
neither rewriting the other — in memory and in the saved chat."""

from __future__ import annotations

import pytest

from jarvis import conversation
from jarvis.db import reset_for_tests as reset_db


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    conversation.reset_for_tests()
    yield
    conversation.reset_for_tests()
    reset_db()


def test_a_result_that_arrives_later_is_its_own_message_after_the_acknowledgement():
    from jarvis.heartbeat import speak
    from jarvis.session import get_active_session_id

    sid = get_active_session_id()
    conversation._push(sid, {"role": "user", "text": "research flights and get back to me"})
    conversation.push_assistant_text(sid, "On it, Boss. I'll come back with the options.")
    speak.speak_now("Boss, \"Flights\" is done. Three options.", reason="A background job finished.")
    texts = [m["text"] for m in conversation._sessions[sid] if m["role"] == "assistant"]
    assert texts == ["On it, Boss. I'll come back with the options.", "Boss, \"Flights\" is done. Three options."]

    from jarvis import chat_store

    saved = [m["text"] for m in chat_store.get_messages(sid) if m["role"] == "assistant"]
    assert saved == texts
