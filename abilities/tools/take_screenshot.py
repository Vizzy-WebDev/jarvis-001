"""Take a screenshot and put the actual picture in the conversation.

The difference from `look_at_screen` is what the user ends up with: that one
answers a question in words and the image is never shown; this one shows the
image and says almost nothing. Two different requests — "what does this say"
versus "send me a screenshot of this" — and collapsing them into one tool makes
both worse.

The picture reaches the transcript through the `ui_action` attachment channel
(see `capabilities/spec.py`'s note and `routes/turn.py`): a tool result may name
a file to show, and the turn stream carries it as its own event.
"""

from __future__ import annotations

from ..capabilities import CapabilitySpec, Risk
from ..control import NoDesktopHere, describe_desktop, get_desktop, pick_window
from ..control.captures import SCREENSHOT, save, url_for


def _run(target: str = "") -> dict:
    desktop = get_desktop()
    try:
        windows = desktop.windows()
    except NoDesktopHere as err:
        return {"ok": False, "error": str(err)}
    except Exception as err:  # noqa: BLE001
        return {"ok": False, "error": f"I couldn't reach the screen right now: {err}"}

    window = pick_window(windows, target)
    if window is None and not windows:
        return {"ok": False, "error": "Nothing appears to be open on screen right now."}

    from ..control.watching import observe

    with observe("Jarvis is taking a screenshot"):
        try:
            shot = desktop.screenshot(window.handle if window else None)
        except Exception as err:  # noqa: BLE001
            return {"ok": False, "error": f"I couldn't take a screenshot right now: {err}"}
        try:
            capture = save(SCREENSHOT, shot.data)
        except Exception as err:  # noqa: BLE001
            return {"ok": False,
                    "error": f"I took the screenshot but couldn't save it: {err}"}

    return {
        "ok": True,
        "note": "Screenshot captured and shown to the user in the chat.",
        "of": window.title if window else "the whole screen",
        "ui_action": {"type": "attachment", "kind": "image",
                      "url": url_for(capture.kind, capture.id),
                      "mimeType": shot.mime_type},
    }


SPEC = CapabilitySpec(
    id="builtin.take_screenshot",
    name="take_screenshot",
    description=("Take a screenshot right now and show it to the user in the chat as an "
                 "actual picture, not a description. Use when they ask to see, share, send "
                 "or save a screenshot. To answer a QUESTION about what's on screen, use "
                 "look_at_screen instead. " + describe_desktop()),
    input_schema={"type": "object", "properties": {
        "target": {"type": "string",
                   "description": "Optional — which window to capture, by app name or part "
                                  "of its title. Leave out for whichever is in front."}},
        "required": []},
    risk=Risk.LOW,
    handler=_run,
    timeout_s=30.0,
    tags=frozenset({"core"}),
)
