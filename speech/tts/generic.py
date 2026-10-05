"""Known voice providers, described as data rather than as one file each.

Different companies' real APIs use different endpoints AND different body field
names — Fish Audio wants `reference_id` for the voice, not `voice` — so a single
one-size-fits-all request shape genuinely cannot work across real services. That
per-company knowledge lives here, once, in code, and is never pushed onto the
user as another field to fill in.

Adding a provider is one entry below: no UI change, no new route, no new file.
Recognised the same typo-tolerant way as any other adapter (`matching.py`).
"""

from __future__ import annotations

from typing import Any, Callable, Iterator

from .. import external_services
from .elevenlabs import NoKey
from .matching import looks_like


def _fish_body(text: str, voice: str | None) -> dict[str, Any]:
    body: dict[str, Any] = {"text": text}
    # Optional: the account's own default voice is used when omitted, the same
    # "don't demand a voice up front" behaviour every provider here keeps.
    if voice:
        body["reference_id"] = voice
    return body


KNOWN_PROVIDERS: list[dict[str, Any]] = [
    {
        "id": "fish-audio",
        "names": ["fishaudio", "fish"],
        "endpoint": "https://api.fish.audio/v1/tts",
        "authHeader": "Authorization",
        "authPrefix": "Bearer ",
        "buildBody": _fish_body,
    },
]

ACCEPT = "audio/mpeg, audio/wav, audio/*;q=0.9, */*;q=0.5"


def _find(ref: str | None) -> dict[str, Any] | None:
    return next((p for p in KNOWN_PROVIDERS if looks_like(ref, p["names"])), None)


def matches_ref(ref: str | None) -> bool:
    return _find(ref) is not None


def is_configured() -> bool:
    return any(_find(s["ref"]) and external_services.get_key(s["ref"])
               for s in external_services.list_services())


def _call(provider: dict[str, Any], api_key: str, text: str, voice: str | None):
    import httpx

    build: Callable[[str, str | None], dict[str, Any]] = provider["buildBody"]
    return httpx.post(provider["endpoint"],
                      headers={provider["authHeader"]: f"{provider['authPrefix']}{api_key}",
                               "Content-Type": "application/json", "Accept": ACCEPT},
                      json=build(text, voice), timeout=120.0)


def stream(text: str, *, voice: str | None = None,
           ref: str | None = None) -> Iterator[dict[str, Any]]:
    provider = _find(ref)
    api_key = external_services.get_key(ref or "") if provider else None
    if not provider or not api_key:
        raise NoKey("No API key is set up for that voice yet.")

    chosen = voice or external_services.get_extra_field(ref or "") or None
    response = _call(provider, api_key, text, chosen)
    if response.status_code != 200:
        detail = (response.text or "")[:300]
        message = (f"{provider['id']} rejected the request ({response.status_code}): {detail}"
                   if detail else f"{provider['id']} rejected the request ({response.status_code}).")
        raise (NoKey if response.status_code in (401, 403) else RuntimeError)(message)
    if not response.content:
        raise RuntimeError(f"{provider['id']} returned no audio.")
    yield {"buffer": response.content,
           "mimeType": response.headers.get("content-type") or "audio/mpeg"}


def test_key(key: str, ref: str | None = None) -> dict[str, Any]:
    """A real check with a short fixed phrase: none of these providers has a
    cheaper standalone "is this key good" call the way ElevenLabs does."""
    provider = _find(ref)
    if provider is None:
        return {"ok": False, "error": "This isn’t a recognised voice provider yet."}
    trimmed = str(key or "").strip()
    if not trimmed:
        return {"ok": False, "error": "No key provided."}
    try:
        response = _call(provider, trimmed, "Testing.",
                         external_services.get_extra_field(ref or "") or None)
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": f"Could not reach {provider['id']}."}
    if response.status_code == 200:
        return {"ok": True}
    detail = (response.text or "")[:200]
    return {"ok": False, "error": (f"Rejected ({response.status_code}): {detail}" if detail
                                   else f"Rejected ({response.status_code}).")}
