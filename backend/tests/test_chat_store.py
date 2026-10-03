"""Chat History persistence — pinning, archiving, and the recycle bin.

Pinning/archiving already had real backend support before this file existed
(`set_pinned()`/`set_archived()`, the sort in `list_conversations()`); this
covers the recycle bin added alongside a frontend UI for pinning that finally
uses it.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from jarvis import chat_store
from jarvis.db import reset_for_tests as reset_db


@pytest.fixture(autouse=True)
def _isolated(scratch):
    from jarvis import session

    reset_db()
    session.reset_for_tests()
    yield
    chat_store.stop_trash_purge()
    session.reset_for_tests()
    reset_db()


def _titled(title: str) -> dict:
    convo = chat_store.create_conversation()
    chat_store.rename_conversation(convo["id"], title)
    return chat_store.get_conversation(convo["id"])


def test_pinned_conversations_sort_first():
    _titled("older")
    pinned = _titled("newer but pinned")
    chat_store.set_pinned(pinned["id"], True)
    listed = chat_store.list_conversations()
    assert listed[0]["id"] == pinned["id"]
    assert listed[0]["pinned"] is True


def test_archived_conversations_are_hidden_by_default_and_isolated_when_asked_for():
    """A previously-real bug: `include_archived=True` used to mean "no filter
    at all" (`1=1`), mixing archived conversations into the SAME list as
    everything else rather than showing just them — there was no way to see
    an archived-only list at all."""
    shown = _titled("visible")
    hidden = _titled("archived")
    chat_store.set_archived(hidden["id"], True)

    assert [c["id"] for c in chat_store.list_conversations()] == [shown["id"]]
    assert [c["id"] for c in chat_store.list_conversations(include_archived=True)] == [hidden["id"]]


# --- truncate_to_before (Edit/Retry's shared cut) --------------------------------


def test_truncate_to_before_deletes_the_message_and_everything_after_it():
    convo = chat_store.create_conversation()
    first = chat_store.append_message(convo["id"], {"role": "user", "text": "one"})
    second = chat_store.append_message(convo["id"], {"role": "assistant", "text": "two"})
    third = chat_store.append_message(convo["id"], {"role": "user", "text": "three"})

    removed = chat_store.truncate_to_before(convo["id"], second["messageId"])

    assert removed == 2
    remaining = chat_store.get_messages(convo["id"])
    assert [m["id"] for m in remaining] == [first["messageId"]]
    assert third["messageId"] not in [m["id"] for m in remaining]


def test_truncate_to_before_an_unknown_id_deletes_nothing():
    convo = chat_store.create_conversation()
    chat_store.append_message(convo["id"], {"role": "user", "text": "one"})

    assert chat_store.truncate_to_before(convo["id"], "m999999") == 0
    assert len(chat_store.get_messages(convo["id"])) == 1


# --- the recycle bin ------------------------------------------------------------

def test_deleting_moves_to_the_bin_rather_than_removing_it():
    convo = _titled("one")
    chat_store.delete_conversation(convo["id"])

    assert chat_store.list_conversations() == []
    assert chat_store.is_conversation(convo["id"])  # still really there
    assert chat_store.is_trashed(convo["id"]) is True
    trashed = chat_store.trash_listed()
    assert len(trashed) == 1 and trashed[0]["id"] == convo["id"]
    assert "deletedAt" in trashed[0]


def test_a_trashed_conversation_never_appears_even_with_archived_shown():
    convo = _titled("one")
    chat_store.set_archived(convo["id"], True)
    chat_store.delete_conversation(convo["id"])
    assert chat_store.list_conversations(include_archived=True) == []


def test_deleting_twice_never_resets_an_earlier_items_own_trash_clock():
    convo = _titled("one")
    chat_store.delete_conversation(convo["id"])
    first_stamp = chat_store.trash_listed()[0]["deletedAt"]

    chat_store.delete_conversation(convo["id"])  # a second delete of the same row
    assert chat_store.trash_listed()[0]["deletedAt"] == first_stamp


def test_restoring_brings_it_back_to_the_active_list():
    convo = _titled("one")
    chat_store.delete_conversation(convo["id"])
    restored = chat_store.restore_conversation(convo["id"])
    assert restored["id"] == convo["id"]
    assert "deletedAt" not in restored
    assert [c["id"] for c in chat_store.list_conversations()] == [convo["id"]]
    assert chat_store.trash_listed() == []


def test_purge_permanently_deletes_regardless_of_trash_state():
    a = _titled("one")
    b = _titled("two")
    chat_store.delete_conversation(a["id"])

    assert chat_store.purge_conversation(a["id"]) is True   # was trashed
    assert chat_store.purge_conversation(b["id"]) is True   # was still active
    assert chat_store.purge_conversation("nope") is False
    assert chat_store.is_conversation(a["id"]) is False
    assert chat_store.is_conversation(b["id"]) is False


def test_empty_trash_removes_only_trashed_items_and_reports_the_count():
    trashed = _titled("one")
    active = _titled("still active")
    chat_store.delete_conversation(trashed["id"])

    assert chat_store.empty_conversation_trash() == 1
    assert chat_store.trash_listed() == []
    assert [c["id"] for c in chat_store.list_conversations()] == [active["id"]]


def test_purge_expired_trash_only_takes_what_is_past_its_time():
    from datetime import datetime, timedelta, timezone

    from jarvis.db import get_db

    old = _titled("old")
    recent = _titled("recent")
    chat_store.delete_conversation(old["id"])
    chat_store.delete_conversation(recent["id"])

    stale = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    get_db().execute("UPDATE conversations SET deleted_at = ? WHERE id = ?", (stale, old["id"]))

    removed = chat_store.purge_expired_conversation_trash()
    assert removed == 1
    remaining = {c["id"] for c in chat_store.trash_listed()}
    assert remaining == {recent["id"]}


def test_a_trashed_conversation_cannot_be_resumed_without_restoring_first():
    from jarvis import session

    convo = _titled("one")
    chat_store.delete_conversation(convo["id"])
    with pytest.raises(LookupError, match="recycle bin"):
        session.activate_conversation(convo["id"])

    chat_store.restore_conversation(convo["id"])
    session.activate_conversation(convo["id"])  # no longer raises


def test_trash_purge_stays_off_without_its_own_interlock(monkeypatch):
    monkeypatch.delenv(chat_store.ENABLE_ENV, raising=False)
    assert chat_store.is_enabled() is False
    assert chat_store.start_trash_purge() is False


# --- the routes ---------------------------------------------------------------

@pytest.fixture
def client():
    from jarvis.main import create_app

    return TestClient(create_app())


def test_deleting_restoring_and_purging_over_http(client):
    # A GET/DELETE against this router lazily creates the very first active
    # conversation the moment nothing is active yet — touched here up front so
    # it doesn't show up as a surprise extra row in the assertions below.
    client.get("/api/conversations")
    convo = _titled("one")

    assert client.delete(f"/api/conversations/{convo['id']}").json() == {"ok": True}
    remaining = {c["id"] for c in client.get("/api/conversations").json()["conversations"]}
    assert convo["id"] not in remaining

    trashed = client.get("/api/conversations/trash").json()["conversations"]
    assert len(trashed) == 1 and trashed[0]["id"] == convo["id"]

    restored = client.post(f"/api/conversations/{convo['id']}/restore").json()
    assert restored["conversation"]["id"] == convo["id"]
    assert client.get("/api/conversations/trash").json()["conversations"] == []

    assert client.delete(f"/api/conversations/{convo['id']}").json() == {"ok": True}
    assert client.delete(f"/api/conversations/{convo['id']}/permanent").json() == {"ok": True}
    assert client.delete("/api/conversations/nope/permanent").status_code == 404
    assert client.get("/api/conversations/trash").json()["conversations"] == []


def test_emptying_the_trash_over_http(client):
    a = _titled("one")
    b = _titled("two")
    chat_store.delete_conversation(a["id"])
    chat_store.delete_conversation(b["id"])

    response = client.delete("/api/conversations/trash").json()
    assert response == {"ok": True, "removed": 2}
    assert client.get("/api/conversations/trash").json()["conversations"] == []


def test_a_trashed_conversation_refuses_to_activate_over_http(client):
    convo = _titled("one")
    chat_store.delete_conversation(convo["id"])
    response = client.post(f"/api/conversations/{convo['id']}/activate")
    assert response.status_code == 404
    assert "recycle bin" in response.json()["error"]
