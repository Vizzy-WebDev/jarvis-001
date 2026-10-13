"""Server-sent events, and the two things that make them survive real use.

**A progress heartbeat is a real typed event, not an SSE comment.** The browser
cannot tell "still working" from "the server died", and two entirely legitimate
phases produce no events for a long time: waiting on a slow model, and a
single slow tool call. Measured on a real install, 102 of 200 kept
notifications were a false "Jarvis seems to have gotten stuck", almost all of
them on a turn that was still legitimately running. A comment (`: ping`) does not
fix that, because the client's own watchdog only re-arms on typed events.

**A closed connection cancels the work.** A tab closing, a network drop, or an
abandoned EventSource must stop the turn rather than let it run to completion
burning quota for a reply nobody will receive.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any, AsyncIterator, Callable, Iterator

from fastapi import Request

HEARTBEAT_S = 10.0
#: How often the producer queue is polled. Small enough that a finished turn ends
#: promptly, large enough not to spin.
_POLL_S = 0.25

_DONE = object()


def frame(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # Nginx and friends buffer text/event-stream by default, which turns a live
    # stream into one delivery at the end.
    "X-Accel-Buffering": "no",
}


async def stream_sync_source(
    request: Request,
    produce: Callable[[threading.Event], Iterator[dict[str, Any]]],
    *,
    heartbeat: Callable[[], dict[str, Any]] | None = None,
) -> AsyncIterator[bytes]:
    """Run a blocking generator on a worker thread and stream what it yields.

    A thread rather than an async generator because the work underneath is
    genuinely blocking — the provider SDKs are synchronous. Bridging through a
    queue is what lets the event loop keep checking whether the client is still
    there, which is the whole point: `cancel` is set the moment it is not.
    """
    loop = asyncio.get_running_loop()
    outbox: asyncio.Queue[Any] = asyncio.Queue()
    cancel = threading.Event()

    def put(item: Any) -> None:
        try:
            loop.call_soon_threadsafe(outbox.put_nowait, item)
        except RuntimeError:  # the loop is gone (shutdown); nobody is listening
            cancel.set()

    def worker() -> None:
        try:
            for event in produce(cancel):
                put(event)
        except Exception as err:  # noqa: BLE001 — a crashed turn must still close the stream
            put({"type": "error", "error": str(err)})
        finally:
            put(_DONE)

    thread = threading.Thread(target=worker, name="sse-turn", daemon=True)
    thread.start()

    # The wait is on the loop itself and the clock is the real one: a heartbeat can no more be held
    # back by a busy worker-thread pool than a counted poll could be.
    last_sent = time.monotonic()
    try:
        while True:
            try:
                item = await asyncio.wait_for(outbox.get(), _POLL_S)
            except asyncio.TimeoutError:
                if await request.is_disconnected():
                    cancel.set()
                    return
                if heartbeat is not None and time.monotonic() - last_sent >= HEARTBEAT_S:
                    last_sent = time.monotonic()
                    yield frame(heartbeat())
                continue

            if item is _DONE:
                return
            last_sent = time.monotonic()
            yield frame(item)
    finally:
        # Covers the paths that do not go through the disconnect check above —
        # the client vanishing mid-write, or the server shutting down.
        cancel.set()
