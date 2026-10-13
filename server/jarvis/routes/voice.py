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

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from ..assembly import get_conversation_mode, get_wake_detector
from ..events import EventType, bus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

#: How long the socket waits for a changed phrase list's model to load before
#: answering "preparing" (loading the models takes a few seconds on a laptop).
LOAD_WAIT_S = 8.0
#: A phrase model is ~200 KB; anything near this is not one.
MAX_MODEL_BYTES = 10 * 1024 * 1024


def listen_for_saved_phrases():
    """The one detector, set to the person's own list of wake phrases."""
    from ..prefs import get_prefs

    from ..voice.wake import DEFAULT_PHRASES

    detector = get_wake_detector()
    # Never edited: the four defaults, the same list the interface starts from
    # (lib/usePrefs.ts). Kept out of the prefs file's defaults, whose shape is pinned.
    phrases = get_prefs().get("wakePhrases", list(DEFAULT_PHRASES))
    if isinstance(phrases, list):
        detector.set_phrases([p for p in phrases if isinstance(p, str)])
    return detector


@router.get("/voice/status")
def status() -> dict[str, Any]:
    detector = listen_for_saved_phrases()
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
    detector = listen_for_saved_phrases()
    mode = get_conversation_mode()

    state = detector.status()  # never waits: loading happens in the background
    waited = 0.0
    while not state["available"] and state["preparing"] and waited < LOAD_WAIT_S:
        # A changed list rebuilds the model in a few seconds: worth waiting for here
        # rather than sending the listener away to try again later.
        await asyncio.sleep(0.25)
        waited += 0.25
        state = detector.status()
    if not state["available"]:
        # Say so and close, rather than accepting audio that will never be
        # scored — a socket that quietly swallows a microphone is worse than a
        # refusal. "preparing" means: ask again shortly.
        await socket.send_json({"type": "preparing" if state["preparing"] else "unavailable",
                                **state})
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


@router.post("/voice/wake/models")
async def add_wake_model(request: Request, name: str = ""):
    """Add a trained wake-phrase model (an openWakeWord `.onnx` file, named after its
    phrase). Raw bytes in the body, like `/uploads`. Answers with the phrase it hears and
    every phrase's state, so the editor can show it at once."""
    body = await request.body()
    if not body:
        return JSONResponse({"error": "No file was sent."}, status_code=400)
    if len(body) > MAX_MODEL_BYTES:
        return JSONResponse({"error": "That file is far too big to be a wake-phrase model."},
                            status_code=400)
    if not name.lower().endswith(".onnx"):
        return JSONResponse({"error": "Choose the .onnx file the training made."}, status_code=400)
    detector = listen_for_saved_phrases()
    try:
        phrase = await asyncio.to_thread(detector.add_model, name, body)
    except ValueError as err:
        return JSONResponse({"error": str(err)}, status_code=400)
    return {"ok": True, "phrase": phrase, "wake": detector.status()}
