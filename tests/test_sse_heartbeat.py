"""A silent stretch of a turn (slow model, long tool) must keep sending progress events on time,
even when the server's worker threads are all busy: the client gives up only on silence."""

from __future__ import annotations

import asyncio
import json
import threading
import time

from jarvis.routes import _sse


class _Req:
    async def is_disconnected(self):
        return False


def _run(coro):
    return asyncio.run(coro)


def test_heartbeats_keep_coming_while_the_default_thread_pool_is_saturated(monkeypatch):
    monkeypatch.setattr(_sse, "HEARTBEAT_S", 0.2)
    release = threading.Event()

    def produce(cancel):
        release.wait(3)
        yield {"type": "done"}

    async def go():
        loop = asyncio.get_running_loop()
        blockers = [loop.run_in_executor(None, release.wait, 3) for _ in range(64)]
        beats, started = [], time.monotonic()
        threading.Timer(1.3, release.set).start()
        async for raw in _sse.stream_sync_source(_Req(), produce, heartbeat=lambda: {"type": "progress"}):
            beats.append((json.loads(raw[6:])["type"], time.monotonic() - started))
        await asyncio.gather(*blockers)
        return beats

    beats = _run(go())
    progress = [t for kind, t in beats if kind == "progress"]
    assert len(progress) >= 3, beats
    assert beats[-1][0] == "done"


def test_a_finished_turn_ends_the_stream_at_once():
    def produce(cancel):
        yield {"type": "done"}

    async def go():
        started = time.monotonic()
        out = [raw async for raw in _sse.stream_sync_source(_Req(), produce)]
        return out, time.monotonic() - started

    out, took = _run(go())
    assert len(out) == 1 and took < 0.2
