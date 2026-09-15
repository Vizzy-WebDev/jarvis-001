"""The event bus (§38).

The properties tested are the ones the Node implementation lacked: a named
vocabulary that rejects typos, isolation so one bad observer cannot break a turn,
and a publisher that never blocks on a slow UI consumer.
"""

from __future__ import annotations

import queue
import threading

import pytest

from jarvis.events import Event, EventBus, EventType


@pytest.fixture
def eb() -> EventBus:
    return EventBus()


def test_subscribers_receive_their_event(eb):
    seen: list[Event] = []
    eb.subscribe(EventType.TOOL_STARTED, seen.append)
    eb.publish(EventType.TOOL_STARTED, {"name": "get_time"})
    assert len(seen) == 1
    assert seen[0].payload["name"] == "get_time"


def test_subscribers_do_not_receive_other_events(eb):
    seen: list[Event] = []
    eb.subscribe(EventType.TOOL_STARTED, seen.append)
    eb.publish(EventType.JOB_CREATED, {})
    assert seen == []


def test_wildcard_receives_everything(eb):
    seen: list[Event] = []
    eb.subscribe(None, seen.append)
    eb.publish(EventType.TOOL_STARTED, {})
    eb.publish(EventType.JOB_CREATED, {})
    assert [e.type for e in seen] == [EventType.TOOL_STARTED, EventType.JOB_CREATED]


def test_an_unknown_event_type_raises_rather_than_vanishing(eb):
    """The failure mode of the string-keyed broadcast this replaces: a typo
    produced an event with no subscribers and no error."""
    with pytest.raises(TypeError):
        eb.publish("tool.strated", {})  # type: ignore[arg-type]


def test_one_failing_subscriber_does_not_stop_the_others(eb):
    """Failure-first (§47). Cost tracking raising must not fail the turn."""
    seen: list[str] = []

    def explodes(_event: Event) -> None:
        raise RuntimeError("observer is broken")

    eb.subscribe(EventType.TOOL_COMPLETED, explodes)
    eb.subscribe(EventType.TOOL_COMPLETED, lambda e: seen.append("second"))
    eb.subscribe(EventType.TOOL_COMPLETED, lambda e: seen.append("third"))

    eb.publish(EventType.TOOL_COMPLETED, {})  # must not raise
    assert seen == ["second", "third"]


def test_unsubscribe_detaches(eb):
    seen: list[Event] = []
    off = eb.subscribe(EventType.JOB_CREATED, seen.append)
    eb.publish(EventType.JOB_CREATED, {})
    off()
    eb.publish(EventType.JOB_CREATED, {})
    assert len(seen) == 1


def test_queue_consumers_receive_events(eb):
    q = eb.subscribe_queue()
    eb.publish(EventType.ASSISTANT_STATE, {"state": "thinking"})
    assert q.get_nowait().payload["state"] == "thinking"


def test_a_full_queue_drops_the_oldest_and_never_blocks(eb):
    """A browser tab that has fallen behind wants current state, not a backlog —
    and must never slow the assistant down."""
    from jarvis.events.bus import QUEUE_MAXSIZE

    q = eb.subscribe_queue()
    for i in range(QUEUE_MAXSIZE + 10):
        eb.publish(EventType.ASSISTANT_STATE, {"n": i})

    assert q.qsize() == QUEUE_MAXSIZE
    # The oldest were dropped, so the first item still queued is not n=0.
    assert q.get_nowait().payload["n"] > 0
    # And the newest survived.
    drained = [q.get_nowait().payload["n"] for _ in range(q.qsize())]
    assert drained[-1] == QUEUE_MAXSIZE + 9


def test_publishing_is_thread_safe(eb):
    seen: list[Event] = []
    lock = threading.Lock()

    def record(event: Event) -> None:
        with lock:
            seen.append(event)

    eb.subscribe(EventType.MODEL_CALL_COMPLETED, record)

    def worker() -> None:
        for _ in range(50):
            eb.publish(EventType.MODEL_CALL_COMPLETED, {})

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(seen) == 200


def test_events_carry_an_id_and_timestamp(eb):
    seen: list[Event] = []
    eb.subscribe(None, seen.append)
    eb.publish(EventType.NOTIFICATION_CREATED, {})
    eb.publish(EventType.NOTIFICATION_CREATED, {})
    assert seen[0].id != seen[1].id
    assert seen[0].at > 0


def test_the_vocabulary_covers_the_directive_events():
    """§38 names the events the UI must be able to subscribe to."""
    values = {e.value for e in EventType}
    for required in (
        "assistant.input", "assistant.thinking", "assistant.response",
        "assistant.speaking", "assistant.interrupted",
        "tool.started", "tool.completed", "tool.failed",
        "job.created", "job.updated", "job.completed",
        "approval.requested", "approval.resolved",
        "system.health_changed", "notification.created",
    ):
        assert required in values, f"§38 requires {required}"
