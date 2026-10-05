"""The voice surface: wake-word audio in, wake events out (§15, §16).

A WebSocket rather than an HTTP post per frame, because this is a continuous
stream of 80 ms chunks and the connection staying open IS the feature — the
browser captures the microphone, the model runs here, and the audio goes nowhere
else.

**What is deliberately NOT here.** No transcription and no reply: a wake is a
signal that the user is talking to the assistant, and what happens next goes
through the same turn pipeline as anything typed, with the same permission gate.
Wiring speech straight into execution from this route is exactly how a voice path
ends up with none of the protections the text path has — the defect this port
exists to fix.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..assembly import get_conversation_mode, get_wake_detector
from ..events import EventType, bus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


@router.get("/voice/status")
def status() -> dict[str, Any]:
    detector = get_wake_detector()
    mode = get_conversation_mode()
    return {
        "wake": detector.status(),
        "mode": mode.mode.value,
        "secondsLeft": round(mode.seconds_left(), 1),
        "idleTimeoutSeconds": mode.idle_timeout_s,
    }


@router.websocket("/voice/wake")
async def wake_socket(socket: WebSocket) -> None:
    """Raw PCM in (16 kHz, 16-bit mono); a JSON message out when the word fires."""
    await socket.accept()
    detector = get_wake_detector()
    mode = get_conversation_mode()

    if not detector.available:
        # Say so and close, rather than accepting audio that will never be
        # scored — a socket that quietly swallows a microphone is worse than a
        # refusal.
        await socket.send_json({"type": "unavailable", **detector.status()})
        await socket.close()
        return

    await socket.send_json({"type": "ready", **detector.status()})
    try:
        while True:
            message = await socket.receive()
            if message.get("type") == "websocket.disconnect":
                break

            audio = message.get("bytes")
            if audio is None:
                # A text frame is a control message, not audio.
                if message.get("text") == "close":
                    mode.close(reason="the user ended it")
                    bus.publish(EventType.VOICE_MODE,
                                {"mode": "asleep", "reason": "the user ended it"})
                continue

            wake = detector.feed(audio)
            if wake is not None:
                transition = mode.wake()
                bus.publish(EventType.VOICE_WAKE,
                            {"model": wake.model, "score": round(wake.score, 4)})
                bus.publish(EventType.VOICE_MODE,
                            {"mode": transition.mode.value, "reason": transition.reason})
                await socket.send_json({"type": "wake", "score": round(wake.score, 4),
                                        "secondsLeft": round(mode.seconds_left(), 1)})
                continue

            closed = mode.tick()
            if closed is not None:
                bus.publish(EventType.VOICE_MODE,
                            {"mode": closed.mode.value, "reason": closed.reason})
                await socket.send_json({"type": "mode", "mode": closed.mode.value,
                                        "reason": closed.reason})
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        logger.exception("wake socket failed")
    finally:
        # The buffer holds audio from a session that is over; scoring it against
        # the next one would splice two different moments together.
        detector.reset()
