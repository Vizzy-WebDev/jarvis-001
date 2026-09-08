"""The two voice sockets: continuous recognition, and a provider's own session.

**The browser never holds a provider key.** It connects here; this server holds
the credential and relays. That is the whole reason both of these exist rather
than the page talking to the service directly.

**`/api/duplex` is recognition ONLY, deliberately.** Reasoning still goes through
the same chat stream every typed message uses, and speech still goes through the
same synthesis route. Keeping this socket to "audio in, transcript out" is what
keeps recognition, reasoning, tools and synthesis separately swappable — fusing
them into one audio-to-audio proxy would collapse that distinction back into what
`/api/live` already is.

**One class of bug from the original does not exist here.** The Node app ran two
WebSocket servers on one HTTP server, each registering its own unconditional
upgrade listener; whichever was constructed first destroyed every request meant
for the other with a 400, which surfaced to the user as "Could not reach the
Jarvis server" with no hint that two sockets were fighting. FastAPI routes a
WebSocket like any other path, so there is nothing to dispatch by hand.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
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

@router.websocket("/duplex")
async def duplex(socket: WebSocket) -> None:
    await socket.accept()

    if not stt.is_configured():
        # Answer rather than refuse: the client needs no separate "can I even
        # try" round trip, and falling back to the browser's own recognition is
        # a real path, not a degraded one.
        await _send(socket, {"type": "ready", "mode": "browser"})
        await socket.close()
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
        await _send(socket, {"type": "ready", "mode": "browser"})
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
    await socket.accept()

    from ..adapters import get_adapter
    from ..assembly import get_registry
    from ..config import get_secret
    from ..prompt import stable_instruction
    from ..voice.options import realtime_models

    # The session comes from whichever connection DECLARED a realtime capability
    # — the same rule the picker uses, applied one layer down, so the two can
    # never disagree. Nothing here names a provider, and nothing here constructs
    # an SDK: opening the session is the adapter's job, for the reason the
    # architecture test spells out.
    entry = next(iter(realtime_models()), None)
    if entry is None or not get_secret(entry.get("secretRef") or ""):
        await _send(socket, {"type": "error",
                             "error": "No model with a realtime voice is set up yet.",
                             "code": "NO_API_KEY"})
        await socket.close()
        return
    # Every tool, not the per-turn budget: a realtime session sets its tool list
    # ONCE at connect with no per-turn refresh, so the usual "core now, find the
    # rest later" split would cap it permanently with no way to reach anything
    # else.
    declarations = [{"name": spec.name, "description": spec.description,
                     "parameters": spec.input_schema}
                    for spec in get_registry().list()]
    config = {
        "response_modalities": ["AUDIO"],
        "tools": [{"function_declarations": declarations}],
        # The STABLE half only. A realtime session has no hook to re-inject a
        # per-turn floor later, so it gets the always-true rules and not the
        # per-turn ones — a real, accepted gap the original documents too.
        "system_instruction": stable_instruction(),
        "input_audio_transcription": {},
        "output_audio_transcription": {},
    }

    try:
        adapter = get_adapter(entry.get("adapter"))
        async with adapter.open_realtime_session(entry, config=config) as session:
            await _send(socket, {"type": "ready"})
            session_id = f"live:{uuid.uuid4().hex[:8]}"
            from_browser = asyncio.create_task(_pump_browser(socket, session))
            from_model = asyncio.create_task(_pump_model(socket, session, session_id))
            done, pending = await asyncio.wait({from_browser, from_model},
                                               return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
    except WebSocketDisconnect:
        return
    except Exception:  # noqa: BLE001
        logger.exception("realtime session failed")
        await _send(socket, {"type": "error", "error": "Could not connect to the realtime voice."})
    finally:
        await _close_socket(socket)


async def _pump_browser(socket: WebSocket, session) -> None:
    while True:
        message = json.loads(await socket.receive_text())
        kind = message.get("type")
        if kind == "audio":
            await session.send_realtime_input(
                media={"data": base64.b64decode(message["data"]), "mime_type": "audio/pcm;rate=16000"})
        elif kind == "text":
            await session.send_client_content(turns=message.get("text"), turn_complete=True)
        elif kind == "end":
            await session.send_realtime_input(audio_stream_end=True)


async def _pump_model(socket: WebSocket, session, session_id: str) -> None:
    """Model → browser, running any tool call through the SAME gate as every
    other turn. A realtime session does not get its own quieter permission
    path: that is exactly how a voice route ends up with none of the
    protections the text one has."""

    async for message in session.receive():
        tool_call = getattr(message, "tool_call", None)
        if tool_call and getattr(tool_call, "function_calls", None):
            responses = []
            for call in tool_call.function_calls:
                await _send(socket, {"type": "tool_start", "name": call.name})
                result = await asyncio.to_thread(_run_capability, call.name,
                                                 dict(call.args or {}), session_id)
                # The same shape the streamed turn emits, so the interface's
                # confirmation chips and navigation handling work identically
                # whichever engine is running.
                await _send(socket, {"type": "tool_result", "name": call.name,
                                     "ok": result.get("ok") is not False,
                                     "summary": result.get("summary"),
                                     "needs_confirmation": result.get("needs_confirmation"),
                                     "ui_action": result.get("ui_action")})
                responses.append({"id": call.id, "name": call.name, "response": result})
            await session.send_tool_response(function_responses=responses)
            continue

        if getattr(message, "data", None):
            await _send(socket, {"type": "audio",
                                 "data": base64.b64encode(message.data).decode("ascii")})

        content = getattr(message, "server_content", None)
        if content is None:
            continue
        heard = getattr(content, "input_transcription", None)
        if heard and getattr(heard, "text", None):
            await _send(socket, {"type": "transcript_in", "text": heard.text})
        spoken = getattr(content, "output_transcription", None)
        if spoken and getattr(spoken, "text", None):
            await _send(socket, {"type": "transcript_out", "text": spoken.text})
        if getattr(content, "interrupted", None):
            await _send(socket, {"type": "interrupted"})
        if getattr(content, "turn_complete", None):
            await _send(socket, {"type": "turn_complete"})


def _run_capability(name: str, args: dict[str, Any], session_id: str) -> dict[str, Any]:
    """One tool call, through the ordinary executor and the ordinary gate."""
    import uuid

    from ..assembly import get_registry
    from ..capabilities.execute import execute
    from ..policy import Autonomy, CallContext, Surface

    ctx = CallContext(session_id=session_id, turn_id=uuid.uuid4().hex,
                      surface=Surface.VOICE, autonomy=Autonomy.INTERACTIVE)
    result = execute(name, args, ctx, registry=get_registry())
    return {"ok": result.ok, "error": result.error, "value": result.value,
            "summary": result.error if not result.ok else None,
            "needs_confirmation": result.approval_id is not None,
            "approvalId": result.approval_id}


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
