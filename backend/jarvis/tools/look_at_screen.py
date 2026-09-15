"""Look at what is on screen and answer a question about it. Nothing else.

Deliberately separate from `control_computer`: this never starts a control
session, never shows the control bar, never touches the mouse or keyboard, and
needs no confirmation — looking is not an action with a side effect. It answers
one question about one moment; for "tell me when X happens" there is
`watch_for`.

Two things it does that a naive version would not:

* **It reads the window's own control tree as well as capturing it.** Real
  labels and values are more exact than a picture, and they are also what lets a
  text-only model answer at all instead of failing the vision requirement.
* **It never looks at Jarvis itself.** The user is normally talking to Jarvis
  through a browser tab, so the front window at the moment "look at my screen"
  runs is very often Jarvis's own — see `desktop.pick_window`.
"""

from __future__ import annotations

import base64

from ..capabilities import CapabilitySpec, Risk
from ..control import NoDesktopHere, describe_desktop, get_desktop, pick_window
from ..control.captures import SCREENSHOT, save

MAX_ELEMENT_LINES = 100


def _element_lines(elements: list) -> str:
    lines = []
    for element in elements[:MAX_ELEMENT_LINES]:
        piece = element.role or "element"
        if element.name:
            piece += f' "{element.name}"'
        if element.value:
            piece += f' = "{element.value[:200]}"'
        lines.append(piece)
    return "\n".join(lines)


def _run(question: str = "", target: str = "") -> dict:
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

    with observe("Jarvis is looking at your screen"):
        try:
            shot = desktop.screenshot(window.handle if window else None)
        except Exception as err:  # noqa: BLE001
            return {"ok": False, "error": f"I couldn't take a look at the screen: {err}"}

        # Kept with the control session's own captures, so there is one place to
        # review anything Jarvis has looked at, whether it acted on it or not.
        try:
            save(SCREENSHOT, shot.data)
        except Exception:  # noqa: BLE001 — not saving it is a lesser failure than not answering
            pass

        elements_text = ""
        if window is not None:
            try:
                # Any real element counts. The original required more than one,
                # because its reader returned the window itself as an entry and
                # "just the window" meant nothing readable; this reader already
                # drops anything with neither a name nor a value, so a single
                # entry is a single piece of genuine content — often the text
                # area holding the whole document.
                elements = desktop.read_window(window.handle)
                if elements:
                    elements_text = _element_lines(elements)
            except Exception:  # noqa: BLE001 — the picture alone is still useful
                elements_text = ""

        asked = str(question or "").strip() or \
            "Describe what you see and share anything worth noting."
        where = (f'The window being looked at is "{window.title}" ({window.process_name}).'
                 if window else
                 "This is a capture of the whole screen — no single window was specified.")
        prompt = (f"{where}\n\n"
                  + (f"Its readable contents:\n{elements_text}\n\n" if elements_text else "")
                  + f"The user asked: {asked}\n\n"
                  "Answer plainly and conversationally, as if glancing over at their screen. "
                  "Do not mention that you were given a screenshot or a list of elements — "
                  "just answer.")

        from ..ai import ask_model

        reply = ask_model(
            prompt,
            media=[{"kind": "image", "mimeType": shot.mime_type,
                    "dataBase64": base64.b64encode(shot.data).decode("ascii")}],
            need={"vision": True})
        if reply.ok:
            return {"ok": True, "answer": reply.text, "sawImage": True}

        # Falling back to text is only meaningful when there IS real window text
        # to answer from; otherwise it is just failing more quietly.
        if elements_text:
            text_only = ask_model(prompt.replace(
                "\n\nThe user asked:",
                "\n\n(No image is available for this model — answering from the window's "
                "text alone.)\n\nThe user asked:"))
            if text_only.ok:
                return {"ok": True, "answer": text_only.text, "sawImage": False}
        return {"ok": False, "error": reply.error or "No model could look at that."}


SPEC = CapabilitySpec(
    id="builtin.look_at_screen",
    name="look_at_screen",
    description=("Look at what is currently visible on the screen and describe or evaluate "
                 "it, without taking any action or controlling anything. Use for \"look at my "
                 "screen and tell me what you think\", \"what does this say\", or \"is this "
                 "showing X\" — never for a task where something should actually be done "
                 "(use control_computer for that). " + describe_desktop()),
    input_schema={"type": "object", "properties": {
        "question": {"type": "string",
                     "description": "What they want to know about what's on screen. If they "
                                    "just said \"look at my screen\", describe generally."},
        "target": {"type": "string",
                   "description": "Optional — which window, by app name or part of its title. "
                                  "Leave out for whichever is in front."}},
        "required": []},
    # Looking changes nothing. The badge is what makes it visible, not a prompt.
    risk=Risk.LOW,
    handler=_run,
    timeout_s=90.0,
    tags=frozenset({"core"}),
)
