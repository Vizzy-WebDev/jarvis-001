"""The server-to-browser event stream (§38).

The UI subscribes; nothing pushes state into it. Every subsystem publishes to the
bus and this route is one more subscriber — which is what stops "tell the UI"
from becoming an import edge from every subsystem to the transport.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from ..events import bus
from ._sse import SSE_HEADERS, frame

router = APIRouter(prefix="/api")

_POLL_S = 0.25
_HEARTBEAT_S = 20.0


@router.get("/events")
async def events(request: Request) -> StreamingResponse:
    async def stream() -> AsyncIterator[bytes]:
        # A bounded queue that drops its OLDEST event when full: a browser tab
        # that has fallen behind wants current state, not a backlog of stale
        # frames, and the publisher must never block on it.
        queue_ = bus.subscribe_queue()
        try:
            yield b": connected\n\n"
            idle = 0.0
            while True:
                if await request.is_disconnected():
                    return
                try:
                    event = queue_.get_nowait()
                except Exception:  # queue.Empty
                    await asyncio.sleep(_POLL_S)
                    idle += _POLL_S
                    if idle >= _HEARTBEAT_S:
                        idle = 0.0
                        yield b": keep-alive\n\n"
                    continue
                idle = 0.0
                yield frame({"type": event.type.value, "at": event.at, **event.payload})
        finally:
            bus.unsubscribe_queue(queue_)

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)
