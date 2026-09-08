"""Real-time speech-to-text, provider-agnostic — the mirror of `tts/`.

One real provider today. The browser's own `SpeechRecognition` is deliberately
NOT a module here: it talks straight to Google from the page with no server
involvement, so there is nothing to proxy. Which mode is active is reported to
the client, and the browser path never touches this package at all.
"""

from __future__ import annotations

from . import deepgram

REF = deepgram.REF


def is_configured() -> bool:
    """Whether a server-proxied provider is available right now. False means the
    client falls back entirely to its own local recognition."""
    return deepgram.is_configured()


def mode() -> str:
    return "deepgram" if is_configured() else "browser"


def test_key(candidate: str, ref: str | None = None):
    return deepgram.test_key(candidate, ref)
