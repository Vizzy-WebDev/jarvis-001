"""Turning a script into a real voice-over file.

Not a new audio system: it is the speech Jarvis already speaks with (`jarvis/tts/`,
whichever voice service the person set up and chose), saved to a file instead of
played, and kept as an artifact like any other deliverable. With no voice service
set up it says so plainly — it never produces an empty or pretend file.

MEDIUM risk: it writes a file, and a paid voice service charges for every
character it is asked to speak, so it is read back before it runs.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from ..artifacts import keep, safe_name
from ..capabilities import CapabilitySpec, Risk

#: Above this, one request is refused rather than sent: voice services cap a
#: single request, and a long script is better narrated section by section.
MAX_CHARACTERS = 4500

_EXTENSION_FOR = {"audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/wav": ".wav",
                  "audio/x-wav": ".wav", "audio/wave": ".wav", "audio/ogg": ".ogg",
                  "audio/opus": ".opus", "audio/aac": ".aac", "audio/flac": ".flac"}

NO_VOICE = ("No voice service is set up, so I can't make audio. Add one (ElevenLabs, for "
            "example) under speech services in Settings and choose it as Jarvis's voice.")


def _run(script: str = "", filename: str = "", voice: str | None = None) -> dict[str, Any]:
    from .. import tts

    text = (script or "").strip()
    if not text:
        return {"ok": False, "error": "There's no script to narrate."}
    if len(text) > MAX_CHARACTERS:
        return {"ok": False, "error": f"That script is {len(text)} characters; one voice-over "
                                      f"can be at most {MAX_CHARACTERS}. Narrate it in sections."}
    if not tts.is_configured():
        return {"ok": False, "error": NO_VOICE}

    buffers: list[bytes] = []
    mime = "audio/mpeg"
    try:
        for chunk in tts.stream(text, voice=voice or None):
            buffers.append(chunk.get("buffer") or b"")
            mime = (chunk.get("mimeType") or mime).split(";")[0].strip().lower()
    except tts.NoKey:
        return {"ok": False, "error": NO_VOICE}
    except Exception as err:  # noqa: BLE001 — the service's own words are the honest answer
        return {"ok": False, "error": f"The voice service couldn't make it: {err}"}
    audio = b"".join(buffers)
    if not audio:
        return {"ok": False, "error": "The voice service returned no audio."}

    extension = _EXTENSION_FOR.get(mime, ".mp3")
    stem = Path(safe_name(filename or "narration")).stem or "narration"
    name = f"{stem}{extension}"
    staging = Path(tempfile.mkdtemp(prefix="jarvis-narration-")) / name
    staging.write_bytes(audio)
    try:
        artifact = keep(staging, name=name)
    except ValueError as err:
        return {"ok": False, "error": str(err)}
    result = artifact.as_result()
    return {"ok": True, **result, "characters": len(text),
            "ui_action": {"type": "attachment", "kind": "audio", "url": result["url"],
                          "mimeType": artifact.mime_type, "name": artifact.name},
            "speak": f"The voice-over {artifact.name} is ready."}


def _summary(args: dict[str, Any]) -> str:
    script = str(args.get("script") or "").strip()
    return (f"Make a voice-over of this {len(script)}-character script with your voice service"
            f" (it may charge per character)? It starts: \"{script[:80]}\"")


SPEC = CapabilitySpec(
    id="builtin.narrate_to_file", name="narrate_to_file",
    description=("Turn a script into a real spoken voice-over audio file, using the voice "
                 "service Jarvis speaks with. For narration, voice-overs and audio versions of "
                 "text. Makes speech only — not music or sound effects."),
    input_schema={"type": "object", "properties": {
        "script": {"type": "string", "description": "Exactly what should be said."},
        "filename": {"type": "string", "description": "A name for the file, without extension."},
        "voice": {"type": "string",
                  "description": "Optional: a specific voice id on the voice service."}},
        "required": ["script"]},
    risk=Risk.MEDIUM,
    handler=_run,
    summarize=_summary,
    timeout_s=180.0,
)
