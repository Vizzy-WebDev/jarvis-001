"""Real-time speech-to-text through Deepgram.

Its ref is created ONCE, deterministically, by the external-service store's own
migration of the key the hardcoded version saved — which is what lets a live
test be keyed by an exact ref here, unlike a voice provider whose ref is
whatever the user typed (see `tts/matching.py`).

The browser's own recognition is the no-key fallback and has no module here at
all: it talks to Google directly from the page, with nothing to proxy.
"""

from __future__ import annotations

from typing import Any

from .. import external_services

REF = "deepgram"
API_BASE = "https://api.deepgram.com"
#: What the browser records and what Deepgram is told to expect. Linear16 at
#: 16kHz is what the client's own capture produces.
LISTEN_QUERY = ("model=nova-2&smart_format=true&interim_results=true"
                "&encoding=linear16&sample_rate=16000&channels=1")


def key() -> str | None:
    return external_services.get_key(REF)


def is_configured() -> bool:
    return bool(key())


def socket_url() -> str:
    return f"wss://api.deepgram.com/v1/listen?{LISTEN_QUERY}"


def test_key(candidate: str, ref: str | None = None) -> dict[str, Any]:
    """A real auth check that transcribes nothing.

    Listing projects is Deepgram's own cheapest authenticated call — the same
    "handshake only, no expensive operation" discipline every provider test in
    this project uses.
    """
    import httpx

    trimmed = str(candidate or "").strip()
    if not trimmed:
        return {"ok": False, "error": "No key provided."}
    try:
        response = httpx.get(f"{API_BASE}/v1/projects",
                             headers={"Authorization": f"Token {trimmed}"}, timeout=30.0)
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "Could not reach Deepgram."}
    if response.status_code == 200:
        return {"ok": True}
    if response.status_code in (401, 403):
        return {"ok": False, "error": "Deepgram rejected that key."}
    return {"ok": False, "error": f"Deepgram rejected the request ({response.status_code})."}
