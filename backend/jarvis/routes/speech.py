"""Speech in and speech out.

`/api/tts` synthesises a whole utterance and answers with the audio; the client
plays sentences as they arrive, so one request per sentence is what the playback
queues on both sides already expect. Every provider's `stream()` is a generator,
so a provider whose API genuinely streams needs no change to this route's
callers — only to this route.

`/api/stt/status` says which recognition mode is live. It is deliberately an
honest report rather than a capability claim: when no key is configured the
answer is that the browser's own recognition is what will run, which is a real,
working path and not a degraded one.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse, Response

from .. import stt, tts

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


@router.get("/tts/providers")
def providers() -> dict[str, Any]:
    """Voices that can actually speak right now.

    Only configured services a real adapter recognises. The browser's own voice
    is not listed here because it has no server side at all — the client always
    offers it, and it is the one that is always available.
    """
    return {"providers": tts.list_providers()}


@router.post("/tts")
def speak(body: dict[str, Any] = Body(default_factory=dict)):
    text = str(body.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "No text provided."}, status_code=400)

    chunks: list[bytes] = []
    mime = "audio/wav"
    try:
        for chunk in tts.stream(text, provider=body.get("provider") or None,
                                voice=body.get("voice") or None):
            chunks.append(chunk["buffer"])
            mime = chunk.get("mimeType") or mime
    except tts.NoKey:
        # A setup problem, not a fault: worded so the user knows what to do.
        return JSONResponse({"error": "No API key is set up for that voice yet.",
                             "code": "NO_API_KEY"}, status_code=400)
    except Exception:  # noqa: BLE001
        logger.exception("speech synthesis failed")
        return JSONResponse({"error": "Could not generate speech audio right now."},
                            status_code=500)

    if not chunks:
        return JSONResponse({"error": "That voice returned no audio."}, status_code=500)
    return Response(content=b"".join(chunks), media_type=mime)


@router.get("/stt/status")
def stt_status() -> dict[str, Any]:
    return {"configured": stt.is_configured()}
