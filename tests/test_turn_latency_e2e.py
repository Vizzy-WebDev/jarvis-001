"""A real turn over a real socket to a real (stub) provider: what Jarvis itself adds to a reply must be
small, and a silent model must not look like a dead server."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from stub_provider_server import StubProvider
from test_shell_e2e import connect_and_select


def _turn(base: str, text: str) -> tuple[list[dict], list[float]]:
    events, stamps = [], []
    with httpx.Client(verify=False, timeout=60) as client:  # plain http: no certificate bundle to load
        started = time.monotonic()
        with client.stream("GET", f"{base}/api/chat/stream", params={"message": text}) as r:
            for line in r.iter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
                    stamps.append(time.monotonic() - started)
    return events, stamps


@pytest.fixture
def stub():
    s = StubProvider("openai-chat", reply="Here you go, Boss.")
    s.start()
    yield s
    s.stop()


def test_jarvis_adds_little_to_a_turn_and_the_first_words_arrive_promptly(live_server, stub):
    connect_and_select(live_server, stub)
    _turn(live_server, "hello")  # warm-up: first use builds registries
    events, stamps = _turn(live_server, "what can you do for me today?")
    kinds = [e["type"] for e in events]
    assert kinds[-1] == "done", kinds
    first_text = next(t for e, t in zip(events, stamps) if e["type"] in ("chunk", "text"))
    assert first_text < 0.6
    assert stamps[-1] < 1.0


def test_a_model_that_goes_quiet_for_a_while_is_not_mistaken_for_a_dead_server(live_server, stub, monkeypatch):
    from jarvis.routes import _sse

    monkeypatch.setattr(_sse, "HEARTBEAT_S", 0.5)
    connect_and_select(live_server, stub)
    _turn(live_server, "hello")  # the first turn of a process builds the tool registry (main() warms it)
    stub.first_token_delay = 2.6
    events, stamps = _turn(live_server, "think about it")
    kinds = [e["type"] for e in events]
    assert kinds[-1] == "done", kinds
    beats = [t for e, t in zip(events, stamps) if e["type"] == "progress"]
    assert len(beats) >= 3, (kinds, stamps)
    gaps = [b - a for a, b in zip([0.0] + beats, beats)]
    assert max(gaps) < 1.5


def test_launching_warms_the_first_turn_up_in_the_background():
    from jarvis import main

    assert callable(main._warm_up)
    main._warm_up()
    from jarvis import assembly

    assert assembly._orchestrator is not None
