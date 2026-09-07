"""Recording the screen, and showing the finished video.

Both halves in one file: they are one feature with two ends, and the pairing —
what start reserves, what stop delivers — is easier to keep right when it is in
front of you. Registered as two capabilities all the same, because the model
calls them separately.

A recording is independent of a control session. Start it, ask for a task to be
done, stop it afterwards: the video keeps running through the whole thing.
"""

from __future__ import annotations

from ..capabilities import CapabilitySpec, Risk
from ..control import recorder


def _start() -> dict:
    result = recorder.start()
    if not result.get("ok"):
        return result
    return {"ok": True,
            "note": "Recording started — say when to stop, or ask for something else and "
                    "stop it later."}


def _stop() -> dict:
    result = recorder.stop()
    if not result.get("ok"):
        return result
    return {
        "ok": True,
        "seconds": result["seconds"],
        "note": "Recording stopped and the video is shown to the user in the chat.",
        "ui_action": {"type": "attachment", "kind": "video", "url": result["url"],
                      "mimeType": "video/mp4"},
    }


START = CapabilitySpec(
    id="builtin.start_screen_recording",
    name="start_screen_recording",
    description=("Start recording the whole screen as a real video, right now. Use when the "
                 "user asks to record their screen or capture a video of something "
                 "happening. It keeps running — including through a computer-control task — "
                 "until stop_screen_recording."),
    input_schema={"type": "object", "properties": {}, "required": []},
    # It captures everything on screen until told to stop, which is more than a
    # glance and worth a deliberate yes.
    risk=Risk.MEDIUM,
    handler=_start,
    timeout_s=30.0,
    tags=frozenset({"core"}),
)

STOP = CapabilitySpec(
    id="builtin.stop_screen_recording",
    name="stop_screen_recording",
    description=("Stop the screen recording that is running and show the user the finished "
                 "video. Use when they say to stop recording or that they're done."),
    input_schema={"type": "object", "properties": {}, "required": []},
    risk=Risk.LOW,
    handler=_stop,
    timeout_s=40.0,
    tags=frozenset({"core"}),
)

SPECS = [START, STOP]
