"""Screen sharing as a mode the user turns on and leaves on.

Different from `look_at_screen` (one glance, one question) and from a monitor
(a watch for one specific condition). Once on, they can ask about what is on
screen at any point without saying "look at my screen" first.

Turning it on never describes anything. Nothing was asked yet — it arms the
badge and adds a line to the prompt, and that is all. The same state the UI
toggle writes, so a spoken instruction and the toggle can never disagree.
"""

from __future__ import annotations

from ..capabilities import CapabilitySpec, Risk
from ..control import watching


def _start() -> dict:
    watching.start_sharing()
    return {"ok": True, "note": "Screen sharing turned on. Don't describe the screen now — "
                                "wait until they ask about something."}


def _stop() -> dict:
    watching.stop_sharing()
    return {"ok": True, "note": "Screen sharing turned off."}


START = CapabilitySpec(
    id="builtin.share_screen",
    name="share_screen",
    description=("Turn on screen sharing — a lasting mode where the user can ask about what's "
                 "on their screen without saying \"look at my screen\" each time. Use for "
                 "\"share my screen with me\" or \"keep an eye on my screen while I work\". "
                 "If they want you to watch for one specific thing and act on it, use "
                 "watch_for instead."),
    input_schema={"type": "object", "properties": {}, "required": []},
    # Nothing is captured or sent by turning it on; it only means a later glance
    # needs no preamble. The badge is what makes it visible.
    risk=Risk.LOW,
    handler=_start,
    tags=frozenset({"core"}),
)

STOP = CapabilitySpec(
    id="builtin.stop_sharing_screen",
    name="stop_sharing_screen",
    description="Turn screen sharing back off. Use when the user says to stop sharing.",
    input_schema={"type": "object", "properties": {}, "required": []},
    risk=Risk.LOW,
    handler=_stop,
    tags=frozenset({"core"}),
)

SPECS = [START, STOP]
