"""Which conversation is active, and the guard against piling up empty ones.

`reset_conversation()` used to have no guard at all: every call, whatever the
current conversation held, inserted a brand-new "New chat" row. Repeated
clicks on the New Chat button with nothing sent yet created an unbounded
string of empty conversations, with nothing to tell them apart.
"""

from __future__ import annotations

import pytest

from jarvis import chat_store, session
from jarvis.db import reset_for_tests as reset_db


@pytest.fixture(autouse=True)
def _isolated(scratch):
    reset_db()
    session.reset_for_tests()
    yield
    session.reset_for_tests()
    reset_db()


def test_new_chat_reuses_an_already_empty_conversation():
    first_id = session.get_active_session_id()
    assert chat_store.has_messages(first_id) is False

    conv = session.reset_conversation()

    assert conv["id"] == first_id, "an empty conversation should be reused, not duplicated"
    assert len(chat_store.list_conversations()) == 1


def test_new_chat_creates_a_fresh_conversation_once_something_was_said():
    first_id = session.get_active_session_id()
    chat_store.append_message(first_id, {"role": "user", "text": "hello"})
    assert chat_store.has_messages(first_id) is True

    conv = session.reset_conversation()

    assert conv["id"] != first_id
    assert len(chat_store.list_conversations()) == 2
    assert session.get_active_session_id() == conv["id"]


def test_repeated_new_chat_clicks_on_an_empty_conversation_never_pile_up():
    session.get_active_session_id()

    session.reset_conversation()
    session.reset_conversation()
    session.reset_conversation()

    assert len(chat_store.list_conversations()) == 1
