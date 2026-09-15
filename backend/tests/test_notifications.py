"""What Jarvis told the user, kept until they have seen it.

The store exists because subsystems were publishing notices to an empty room:
a scheduled task that failed overnight announced itself on the bus and nothing
was listening. The observer is the listener, and nothing in the scheduler, the
monitor or the heartbeat had to change for it — which is the event seam earning
its keep.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from jarvis import notifications
from jarvis.events import EventType
from jarvis.events.bus import EventBus
from jarvis.observers import start_observers, stop_observers


@pytest.fixture(autouse=True)
def _isolated(scratch):
    yield
    stop_observers()
    notifications.stop_trash_purge()


@pytest.fixture
def client():
    from jarvis.main import create_app

    return TestClient(create_app())


def test_a_notice_is_kept_and_reads_back():
    saved = notifications.add(kind="task_run", level="warning",
                              title="Morning briefing didn't run.",
                              body="No model was available.")
    assert saved["id"].startswith("n")
    assert saved["read"] is False and saved["count"] == 1

    [row] = notifications.listed()
    assert row["title"] == "Morning briefing didn't run."
    assert notifications.unread_count() == 1


def test_the_same_fault_repeating_collapses_into_one_row_with_a_count():
    """The real case this exists for: a watchdog firing on nearly every turn
    while the whole roster was rate-limited put 102 identical rows into a
    200-row cap, evicting the findings worth reading."""
    for _ in range(5):
        notifications.add(kind="system", title="Nothing could answer that.")

    rows = notifications.listed()
    assert len(rows) == 1
    assert rows[0]["count"] == 5
    assert notifications.unread_count() == 1


def test_a_repeat_after_it_was_read_gets_its_own_row():
    """Once they have seen and dismissed it, the same fault happening again is
    news, not a repeat."""
    first = notifications.add(kind="system", title="Nothing could answer that.")
    notifications.mark_read(first["id"])
    notifications.add(kind="system", title="Nothing could answer that.")
    assert len(notifications.listed()) == 2


def test_two_unrelated_notices_are_never_merged():
    notifications.add(kind="system", title="A task failed.")
    notifications.add(kind="monitor", title="A task failed.")     # different kind
    notifications.add(kind="system", title="A different thing.")  # different title
    assert len(notifications.listed()) == 3


def test_a_repeat_keeps_the_freshest_detail():
    notifications.add(kind="system", title="Nothing could answer that.",
                      body="every model was rate-limited")
    notifications.add(kind="system", title="Nothing could answer that.",
                      body="the key was rejected")
    [row] = notifications.listed()
    assert row["body"] == "the key was rejected"


def test_the_store_does_not_grow_without_limit(monkeypatch):
    monkeypatch.setattr(notifications, "MAX_KEPT", 5)
    for n in range(12):
        notifications.add(kind="system", title=f"notice {n}")
    rows = notifications.listed()
    assert len(rows) == 5
    # Newest first, and it is the newest that survive.
    assert rows[0]["title"] == "notice 11"


def test_a_notice_with_nothing_to_say_is_refused():
    with pytest.raises(ValueError):
        notifications.add(title="")


def test_marking_read_and_clearing():
    a = notifications.add(kind="system", title="one")
    notifications.add(kind="system", title="two")
    notifications.mark_read(a["id"])
    assert notifications.unread_count() == 1

    notifications.mark_all_read()
    assert notifications.unread_count() == 0

    assert notifications.remove(a["id"]) is True
    assert notifications.remove("nope") is False
    notifications.clear_all()
    assert notifications.listed() == []


# --- the recycle bin ------------------------------------------------------------

def test_remove_and_clear_move_to_the_bin_rather_than_deleting():
    a = notifications.add(kind="system", title="one")
    notifications.add(kind="system", title="two")

    notifications.remove(a["id"])
    assert [row["title"] for row in notifications.listed()] == ["two"]  # "one" left the active view...
    assert len(notifications.trash_listed()) == 1  # ...but recoverable

    notifications.clear_all()
    assert notifications.listed() == []
    assert len(notifications.trash_listed()) == 2


def test_deleting_something_already_in_the_bin_still_reports_success():
    a = notifications.add(kind="system", title="one")
    assert notifications.remove(a["id"]) is True
    assert notifications.remove(a["id"]) is True  # already trashed, still a real row
    assert notifications.remove("nope") is False


def test_clearing_twice_never_resets_an_earlier_items_own_trash_clock():
    a = notifications.add(kind="system", title="one")
    notifications.remove(a["id"])
    first_stamp = notifications.trash_listed()[0]["trashedAt"]

    notifications.add(kind="system", title="two")
    notifications.clear_all()  # must not touch "one"'s own trashedAt
    trashed = {row["id"]: row["trashedAt"] for row in notifications.trash_listed()}
    assert trashed[a["id"]] == first_stamp


def test_restoring_brings_it_back_to_the_active_list():
    a = notifications.add(kind="system", title="one")
    notifications.remove(a["id"])
    assert notifications.restore(a["id"]) is True
    assert [row["id"] for row in notifications.listed()] == [a["id"]]
    assert notifications.trash_listed() == []


def test_restoring_something_not_in_the_bin_fails_honestly():
    a = notifications.add(kind="system", title="one")
    assert notifications.restore(a["id"]) is False   # never trashed
    assert notifications.restore("nope") is False    # does not exist


def test_purge_permanently_deletes_regardless_of_trash_state():
    a = notifications.add(kind="system", title="one")
    b = notifications.add(kind="system", title="two")
    notifications.remove(a["id"])

    assert notifications.purge(a["id"]) is True   # was trashed
    assert notifications.purge(b["id"]) is True   # was still active
    assert notifications.purge("nope") is False
    assert notifications.listed(include_trashed=True) == []


def test_empty_trash_removes_only_trashed_items_and_reports_the_count():
    a = notifications.add(kind="system", title="one")
    notifications.add(kind="system", title="still active")
    notifications.remove(a["id"])

    assert notifications.empty_trash() == 1
    assert notifications.trash_listed() == []
    assert len(notifications.listed()) == 1


def test_purge_expired_trash_only_takes_what_is_past_its_time(monkeypatch):
    from datetime import datetime, timedelta, timezone

    a = notifications.add(kind="system", title="old")
    b = notifications.add(kind="system", title="recent")
    notifications.remove(a["id"])
    notifications.remove(b["id"])

    stale = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    data = notifications._load()
    for row in data["notifications"]:
        if row["id"] == a["id"]:
            row["trashedAt"] = stale
    notifications.write_json(notifications.FILE, data)

    removed = notifications.purge_expired_trash()
    assert removed == 1
    remaining = {row["id"] for row in notifications.trash_listed()}
    assert remaining == {b["id"]}


def test_trash_purge_stays_off_without_its_own_interlock(monkeypatch):
    monkeypatch.delenv(notifications.ENABLE_ENV, raising=False)
    assert notifications.is_enabled() is False
    assert notifications.start_trash_purge() is False


# --- the observer -------------------------------------------------------------

def test_what_a_subsystem_publishes_is_written_down():
    """Nothing in the scheduler or the monitor changed for this. They publish
    what they always published; the difference is that something listens."""
    bus = EventBus()
    start_observers(bus)
    bus.publish(EventType.NOTIFICATION_CREATED, {
        "kind": "monitor", "level": "info",
        "title": "What you asked me to watch for happened: the export finished.",
        "body": "", "meta": {"monitorId": "m1"}})

    [row] = notifications.listed()
    assert row["kind"] == "monitor" and "export finished" in row["title"]
    assert row["meta"] == {"monitorId": "m1"}


def test_a_published_notice_with_no_title_is_not_stored():
    bus = EventBus()
    start_observers(bus)
    bus.publish(EventType.NOTIFICATION_CREATED, {"kind": "system", "body": "orphaned"})
    assert notifications.listed() == []


def test_storing_one_announces_it_to_open_tabs():
    """A separate event from the one that caused it: a store that re-published
    what it consumed would feed itself, and a browser wants the row — with its
    id, so it can be marked read — not the request that made it."""
    bus = EventBus()
    seen = []
    bus.subscribe(EventType.NOTIFICATION_STORED, lambda event: seen.append(event.payload))
    notifications.add(kind="system", title="hello", event_bus=bus)

    assert seen and seen[0]["notification"]["title"] == "hello"
    assert seen[0]["notification"]["id"]


def test_the_observer_does_not_loop_on_itself():
    bus = EventBus()
    start_observers(bus)
    bus.publish(EventType.NOTIFICATION_CREATED, {"kind": "system", "title": "once"})
    assert len(notifications.listed()) == 1
    assert notifications.listed()[0]["count"] == 1


# --- the routes ---------------------------------------------------------------

def test_reading_marking_and_clearing_over_http(client):
    notifications.add(kind="system", title="one")
    second = notifications.add(kind="system", title="two")

    listed = client.get("/api/notifications").json()["notifications"]
    assert [row["title"] for row in listed] == ["two", "one"]

    client.post("/api/notifications/read", json={"ids": [second["id"]]})
    assert notifications.unread_count() == 1
    client.post("/api/notifications/read-all")
    assert notifications.unread_count() == 0

    assert client.delete(f"/api/notifications/{second['id']}").json() == {"ok": True}
    assert client.delete("/api/notifications/nope").status_code == 404
    assert client.delete("/api/notifications").json() == {"ok": True}
    assert client.get("/api/notifications").json() == {"notifications": []}


def test_the_bell_can_ask_for_just_the_newest_few(client):
    for n in range(10):
        notifications.add(kind="system", title=f"notice {n}")
    listed = client.get("/api/notifications", params={"limit": 3}).json()["notifications"]
    assert len(listed) == 3 and listed[0]["title"] == "notice 9"


def test_posting_one_without_a_title_is_a_400(client):
    assert client.post("/api/notifications", json={"body": "no title"}).status_code == 400
