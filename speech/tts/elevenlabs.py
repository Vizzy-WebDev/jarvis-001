"""ElevenLabs speech.

Its key comes from the generic external-service store, never a new hardcoded
secret. Recognition of WHICH configured service is this one lives here rather
than in the shared seam: provider-specific knowledge belongs in the adapter that
is already calling this company's API, so the seam itself hardcodes no names.

**The default voice is resolved, never assumed.** A hardcoded "standard" voice
id was tried and fails live on a free-tier account — "Free users cannot use
library voices via the API". There is no voice id guaranteed synthesizable on
every plan, so with no Voice ID set the default is the FIRST voice in the
account's own list: one this account provably has access to. Cached briefly so a
multi-sentence reply does not re-fetch the list per sentence.
"""

from __future__ import annotations

import json
import time
from typing import Any, Iterator

from .. import external_services
from .matching import looks_like

API_BASE = "https://api.elevenlabs.io"
CANONICAL_NAMES = ["elevenlabs", "11labs"]
DEFAULT_VOICE_CACHE_S = 5 * 60

_default_voice: dict[str, tuple[str, float]] = {}


class NoKey(RuntimeError):
    """No key is configured for this provider — a setup problem, not a fault."""


def matches_ref(ref: str | None) -> bool:
    return looks_like(ref, CANONICAL_NAMES)


def _find_ref() -> str | None:
    return next((s["ref"] for s in external_services.list_services()
                 if matches_ref(s["ref"])), None)


def is_configured() -> bool:
    ref = _find_ref()
    return bool(ref and external_services.get_key(ref))


def resolved_key() -> dict[str, str] | None:
    """The `{ref, key}` this adapter matched, reusing the SAME recognition
    rather than a second lookup that could disagree about which service is
    the ElevenLabs one. Used by balance polling as well as synthesis."""
    ref = _find_ref()
    if not ref:
        return None
    key = external_services.get_key(ref)
    return {"ref": ref, "key": key} if key else None


def _message(body: str, status: int) -> str:
    try:
        parsed = json.loads(body)
        detail = parsed.get("detail")
        if isinstance(detail, dict) and detail.get("message"):
            return str(detail["message"])
        if isinstance(detail, str):
            return detail
    except (ValueError, AttributeError):
        pass
    return ("ElevenLabs rejected the API key." if status == 401
            else f"ElevenLabs rejected the request ({status}).")


def _default_voice_id(api_key: str) -> str:
    import httpx

    cached = _default_voice.get(api_key)
    if cached and time.time() - cached[1] < DEFAULT_VOICE_CACHE_S:
        return cached[0]

    response = httpx.get(f"{API_BASE}/v1/voices", headers={"xi-api-key": api_key}, timeout=30.0)
    if response.status_code != 200:
        raise (NoKey if response.status_code == 401 else RuntimeError)(
            _message(response.text, response.status_code))
    voices = (response.json() or {}).get("voices") or []
    if not voices or not voices[0].get("voice_id"):
        raise RuntimeError("This ElevenLabs account has no voices available — add one in your "
                           "ElevenLabs account, or set a Voice ID on the saved service.")
    voice_id = str(voices[0]["voice_id"])
    _default_voice[api_key] = (voice_id, time.time())
    return voice_id


def stream(text: str, *, voice: str | None = None,
           ref: str | None = None) -> Iterator[dict[str, Any]]:
    """One chunk: a complete, playable MP3.

    A generator even though this call is not itself streamed, because that is
    the contract every provider keeps — a genuinely streaming provider added
    later needs no change in any caller.
    """
    import httpx

    found = _find_ref()
    api_key = external_services.get_key(found) if found else None
    if not api_key:
        raise NoKey("No ElevenLabs API key configured.")

    voice_id = (voice or (found and external_services.get_extra_field(found))
                or _default_voice_id(api_key))
    response = httpx.post(
        f"{API_BASE}/v1/text-to-speech/{voice_id}",
        headers={"xi-api-key": api_key, "Content-Type": "application/json",
                 "Accept": "audio/mpeg"},
        json={"text": text, "model_id": "eleven_multilingual_v2"}, timeout=120.0)
    if response.status_code != 200:
        message = _message(response.text, response.status_code)
        raise (NoKey if response.status_code == 401 else RuntimeError)(message)

    yield {"buffer": response.content, "mimeType": "audio/mpeg"}


def test_key(key: str, ref: str | None = None) -> dict[str, Any]:
    """A real auth check that costs no synthesis — the same "handshake only"
    discipline every other provider's test uses."""
    import httpx

    trimmed = str(key or "").strip()
    if not trimmed:
        return {"ok": False, "error": "No key provided."}
    try:
        response = httpx.get(f"{API_BASE}/v1/user", headers={"xi-api-key": trimmed}, timeout=30.0)
    except Exception:  # noqa: BLE001 — unreachable is an answer
        return {"ok": False, "error": "Could not reach ElevenLabs."}
    if response.status_code == 200:
        return {"ok": True}
    return {"ok": False, "error": _message(response.text, response.status_code)}


def reset_for_tests() -> None:
    _default_voice.clear()
