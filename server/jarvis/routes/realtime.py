"""The two voice sockets: continuous recognition, and a provider's own session
(which has no implementation until the model system is rebuilt).

**The browser never holds a provider key.** It connects here; this server holds
the credential and relays. That is the whole reason both of these exist rather
than the page talking to the service directly.

**`/api/duplex` is recognition ONLY, deliberately.** Reasoning still goes through
the same chat stream every typed message uses, and speech still goes through the
same synthesis route. Keeping this socket to "audio in, transcript out" is what
keeps recognition, reasoning, tools and synthesis separately swappable — fusing
them into one audio-to-audio proxy would collapse that distinction back into what
`/api/live` already is.

**Two sockets on one server need no hand-written upgrade dispatch.** FastAPI routes a
WebSocket like any other path, so there is no unconditional upgrade listener that could
destroy requests meant for the other socket.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .. import stt

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

async def _send(socket: WebSocket, payload: dict[str, Any]) -> None:
    try:
        await socket.send_text(json.dumps(payload))
    except (WebSocketDisconnect, RuntimeError):
        pass  # the browser hung up mid-write; nothing to recover


# --- continuous recognition ---------------------------------------------------

#: Which recogniser listens: `local` (on this computer, the default) or `deepgram`. Read
#: from the person's prefs; not in the prefs defaults, whose shape is pinned.
LISTENING_PREF = "listeningProvider"


def listening_choice() -> str:
    from ..prefs import get_prefs

    choice = get_prefs().get(LISTENING_PREF) or "local"
    return choice if choice in ("local", "deepgram") else "local"


@router.websocket("/duplex")
async def duplex(socket: WebSocket) -> None:
    await socket.accept()

    choice = listening_choice()
    if choice == "local":
        await _listen_on_this_computer(socket)
        return
    if not stt.is_configured():
        # The person chose the cloud service and its key is gone: say so, never
        # quietly listen some other way.
        await _send(socket, {"type": "error", "fatal": True, "error": (
            "Deepgram is chosen for listening, but its key is not set up. Add it in "
            "Settings → Voice, or choose On this computer.")})
        await _close_socket(socket)
        return

    client_closed = False
    # ONE server-side reconnect per real disconnect, cleared once a replacement
    # session is confirmed healthy. This exists because of a real report: the
    # recognition session died while this outer socket stayed open the whole
    # time, so nothing on the browser side ever saw a close — the mic just kept
    # sending frames into a dead session for the rest of the session.
    reconnecting = False

    async def relay(session) -> None:
        async for raw in session:
            try:
                message = json.loads(raw)
            except (ValueError, TypeError):
                continue
            kind = message.get("type")
            if kind == "Results":
                alternative = ((message.get("channel") or {}).get("alternatives") or [{}])[0]
                await _send(socket, {
                    "type": "transcript",
                    "text": alternative.get("transcript") or "",
                    "isFinal": bool(message.get("is_final")),
                    "speechFinal": bool(message.get("speech_final")),
                    "confidence": alternative.get("confidence"),
                })
            elif kind == "UtteranceEnd":
                await _send(socket, {"type": "utterance_end"})
            elif kind == "SpeechStarted":
                await _send(socket, {"type": "speech_started"})

    session = None
    pump: asyncio.Task | None = None
    try:
        session = await stt.open_session()
        await _send(socket, {"type": "ready", "mode": "deepgram"})
        pump = asyncio.create_task(relay(session))

        while True:
            message = json.loads(await socket.receive_text())
            if message.get("type") == "audio" and isinstance(message.get("data"), str):
                if pump is not None and pump.done() and not client_closed and not reconnecting:
                    # The session died under a socket that never closed. One
                    # replacement, on this same browser connection: the mic
                    # never stopped and never needs to know.
                    reconnecting = True
                    try:
                        session = await stt.open_session()
                        pump = asyncio.create_task(relay(session))
                        reconnecting = False
                    except Exception:  # noqa: BLE001
                        logger.exception("could not reopen recognition")
                        await _send(socket, {"type": "error",
                                             "error": "Speech recognition connection closed."})
                        break
                if session is not None:
                    await session.send(base64.b64decode(message["data"]))
            elif message.get("type") == "end":
                break
    except WebSocketDisconnect:
        client_closed = True
    except stt.deepgram.NoKey:
        await _send(socket, {"type": "error", "fatal": True,
                             "error": "Deepgram's key is not set up. Add it in Settings → Voice."})
    except Exception:  # noqa: BLE001
        logger.exception("recognition relay failed")
        await _send(socket, {"type": "error", "error": "Could not start speech recognition."})
    finally:
        if pump is not None:
            pump.cancel()
        if session is not None:
            await _close_quietly(session)
        if not client_closed:
            await _close_socket(socket)


# --- a provider's own realtime session ----------------------------------------

@router.websocket("/live")
async def live(socket: WebSocket) -> None:
    """A provider's own speech-to-speech session. None exists at the moment: the
    old implementation was removed along with the AI model system, and the
    rebuilt one will bring this back."""
    await socket.accept()
    await _send(socket, {"type": "error",
                         "error": "No model with a realtime voice is set up yet.",
                         "code": "NO_API_KEY"})
    await _close_socket(socket)


async def _close_quietly(session) -> None:
    try:
        await session.close()
    except Exception:  # noqa: BLE001 — already gone is the normal case
        pass


async def _close_socket(socket: WebSocket) -> None:
    try:
        await socket.close()
    except (RuntimeError, WebSocketDisconnect):
        pass


async def _listen_on_this_computer(socket: WebSocket) -> None:
    """Recognition with no network at all: the same messages as the cloud relay above.

    The models are fetched the first time they are needed; until they are here the browser is
    sent `downloading` progress (bytes so far, bytes in all) on this same socket, then
    `loading`, then `ready` — so the screen can show a real bar and the session simply
    starts when it can."""
    from .. import speech_models
    from ..stt import local

    client_closed = False
    session = None
    pump: asyncio.Task | None = None
    try:
        if not local.available():
            await _send(socket, {"type": "error", "fatal": True, "error": (
                "On-device speech recognition is not installed yet. Restart Jarvis to finish "
                "installing it.")})
            return
        model_id = _local_model()
        needed = ("silero-vad", model_id)
        while True:
            states = [speech_models.ensure(m) for m in needed]
            failed = next((s for s in states if s["state"] == "failed"), None)
            if failed:
                await _send(socket, {"type": "error", "fatal": True,
                                     "error": f"{failed['label']}: {failed['error']}"})
                return
            if all(s["state"] == "ready" for s in states):
                break
            await _send(socket, {"type": "downloading",
                                 "label": next(s["label"] for s in states if s["state"] != "ready"),
                                 "done": sum(s["done"] for s in states),
                                 "total": sum(s["total"] for s in states)})
            await asyncio.sleep(0.5)

        await _send(socket, {"type": "loading"})
        session = local.LocalSession(model_id)
        await session.warm()
        await _send(socket, {"type": "ready", "mode": "local", "model": model_id})

        async def relay() -> None:
            async for event in session:
                await _send(socket, event)

        pump = asyncio.create_task(relay())
        while True:
            message = json.loads(await socket.receive_text())
            if message.get("type") == "audio" and isinstance(message.get("data"), str):
                await session.send(base64.b64decode(message["data"]))
            elif message.get("type") == "end":
                break
    except WebSocketDisconnect:
        client_closed = True
    except Exception:  # noqa: BLE001
        logger.exception("on-device recognition failed")
        await _send(socket, {"type": "error", "error": "Could not start speech recognition."})
    finally:
        if session is not None:
            await session.close()
        if pump is not None:
            pump.cancel()
        if not client_closed:
            await _close_socket(socket)


def _local_model() -> str:
    from ..prefs import get_prefs
    from ..stt import local

    chosen = get_prefs().get("localListeningModel")
    return chosen if chosen in local.MODELS else local.DEFAULT_MODEL
