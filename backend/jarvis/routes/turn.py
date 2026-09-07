"""The chat turn over HTTP.

`GET`, not `POST`, because the browser's `EventSource` can only make a GET with
no body — the same reason the original does. The message rides in the query
string; attachments will ride there as ids, never paths.

The turn events are translated to a small wire vocabulary here rather than
leaking the orchestrator's own dataclasses: the front end should not have to
change shape because an internal type gained a field.
"""

from __future__ import annotations

import logging
import threading
import uuid
from typing import Any, Iterator

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..assembly import get_orchestrator
from ..orchestrator import (
    ApprovalRequired, Chunk, Done, Failed, Interrupted, Routed, Switched, ToolRan, TurnRequest,
)
from ..policy import Autonomy, Surface
from ..session import get_active_session_id
from ._sse import SSE_HEADERS, stream_sync_source

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

#: Below this, a voice transcript is treated as possibly misheard — which can
#: only ever ADD a confirmation, never remove one (see policy/decide.py).
LOW_CONFIDENCE_BELOW = 0.6


def to_wire(event: Any) -> dict[str, Any]:
    if isinstance(event, Chunk):
        return {"type": "chunk", "text": event.text}
    if isinstance(event, Done):
        return {"type": "done", "text": event.text, "steps": event.steps}
    if isinstance(event, Routed):
        return {"type": "routed", "intent": event.intent.value, "fast": event.fast,
                "confidence": event.confidence, "reason": event.reason}
    if isinstance(event, ToolRan):
        return {"type": "tool_result", "capability": event.capability, "ok": event.ok,
                "outcome": event.outcome.value, "error": event.error}
    if isinstance(event, ApprovalRequired):
        return {"type": "approval_required", "approvalId": event.approval_id,
                "capability": event.capability, "reason": event.reason}
    if isinstance(event, Switched):
        return {"type": "model_switch", "to": event.to_model, "from": event.from_model,
                "reason": event.reason}
    if isinstance(event, Interrupted):
        return {"type": "interrupted", "spokenText": event.spoken_text}
    if isinstance(event, Failed):
        wire = {"type": "error", "error": event.error}
        if event.code:
            wire["code"] = event.code
            wire["detail"] = event.detail
        return wire
    return {"type": "unknown"}


def _phase_of(event: dict[str, Any]) -> str:
    return {"tool_result": "tool", "model_switch": "model"}.get(event["type"], "thinking")


@router.get("/chat/stream")
async def chat_stream(request: Request, message: str = "", source: str = "text",
                      confidence: float | None = None, attachments: str = ""):
    text = (message or "").strip()
    # Attachment ids ride in the query string because EventSource can only make a
    # GET with no body. Ids, never paths — see routes/uploads.py.
    attached = tuple(part.strip() for part in (attachments or "").split(",") if part.strip())

    # Sending a photo with no words is an ordinary thing to do, so an empty
    # message is only an error when nothing is attached either.
    if not text and not attached:
        return JSONResponse({"error": "No message provided."}, status_code=400)

    surface = Surface.VOICE if source == "voice" else Surface.TEXT
    low_confidence = (surface is Surface.VOICE and confidence is not None
                      and confidence < LOW_CONFIDENCE_BELOW)
    turn = TurnRequest(
        text=text,
        session_id=get_active_session_id(),
        surface=surface,
        autonomy=Autonomy.INTERACTIVE,
        low_confidence=low_confidence,
        turn_id=uuid.uuid4().hex,
        attachments=attached,
    )

    phase = {"value": "thinking"}

    def produce(cancel: threading.Event) -> Iterator[dict[str, Any]]:
        for event in get_orchestrator().run_turn(turn, cancel):
            wire = to_wire(event)
            phase["value"] = _phase_of(wire)
            yield wire

    return StreamingResponse(
        stream_sync_source(request, produce,
                           heartbeat=lambda: {"type": "progress", "phase": phase["value"]}),
        media_type="text/event-stream", headers=SSE_HEADERS)
