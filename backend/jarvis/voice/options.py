"""What can actually listen and speak right now.

**The rule this file exists to enforce: an engine or a voice is offered because
a capability check says it is available, never because this code knows a
provider's name.** Nowhere below is a brand compared against a string. A
realtime engine appears when some connection's adapter DECLARES `realtime` and
that connection is ready to use; if another provider ships a realtime API later
it appears by declaring the same flag, with no change here.

Three separate choices, deliberately not collapsed into one:

1. **Which model answers** — any configured model, exactly as text already does.
   The gateway routes it; nothing in the voice path touches that decision.
2. **Which voice speaks** — any configured provider, plus the browser's own,
   which is free, offline, needs no key and is therefore always available.
3. **Which engine runs the loop** — a pipeline turn, continuous listening, or a
   provider's own realtime session.
"""

from __future__ import annotations

from typing import Any

from .. import stt, tts
from ..adapters import get_capabilities
from ..gateway import connections, registry


def _ready_models() -> list[dict[str, Any]]:
    return [m for m in registry.list_models() if m.get("enabled") and registry.is_ready(m)]


def realtime_models() -> list[dict[str, Any]]:
    """Every ready model whose adapter declares a realtime API of its own."""
    return [m for m in _ready_models() if get_capabilities(m.get("adapter")).get("realtime")]


def list_engines() -> list[dict[str, Any]]:
    """The loops that can run, each with why it can or cannot.

    A reason is always given for an unavailable engine. "Not available" with no
    explanation is the shape of thing people file bugs about, and the answer is
    almost always something they could have fixed in ten seconds.
    """
    ready = _ready_models()
    realtime = realtime_models()

    return [
        {
            "id": "pipeline",
            "label": "Speak and listen",
            "description": "You talk, Jarvis answers. Uses whichever model is chosen for text.",
            "available": bool(ready),
            "reason": None if ready else "Add a model first.",
        },
        {
            "id": "duplex",
            "label": "Full duplex",
            "description": "Keeps listening while it talks, so you can interrupt it.",
            "available": bool(ready),
            "reason": None if ready else "Add a model first.",
        },
        {
            "id": "realtime",
            "label": "Realtime voice",
            "description": "A provider's own speech-to-speech session — the fastest, "
                           "when a model that offers one is connected.",
            "available": bool(realtime),
            # Named from the models themselves, so this stays true if the set of
            # realtime-capable providers ever changes.
            "models": [{"id": m["id"], "label": m["label"]} for m in realtime],
            "reason": None if realtime else
                      "None of your models offers a realtime voice session.",
        },
    ]


def list_voices() -> list[dict[str, Any]]:
    """Every voice that can speak, the always-available one first.

    The browser's voice has no server side at all — it is not a provider in the
    speech seam and never will be — which is exactly why it is the one that is
    always here, with no key and no account.
    """
    voices: list[dict[str, Any]] = [{
        "id": "browser",
        "label": "Your browser’s own voice",
        "configured": True,
        "needsKey": False,
    }]
    voices += [{**provider, "needsKey": True} for provider in tts.list_providers()]
    return voices


def status() -> dict[str, Any]:
    """One read for the whole picker."""
    return {
        "engines": list_engines(),
        "voices": list_voices(),
        "listening": {
            "mode": stt.mode(),
            # True means a server-proxied provider is live. False is not a
            # degraded state: the browser's own recognition is a real path.
            "serverProxied": stt.is_configured(),
        },
        "connections": len(connections.list_connections()),
    }
