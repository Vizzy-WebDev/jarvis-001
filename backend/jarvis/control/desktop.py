"""The desktop itself: windows, the mouse, the keyboard, the screen.

One seam, two implementations, chosen the way `sandbox/runner.py` chooses a
backend: probe once, report honestly, and put what is NOT available into the
description a tool shows the user. `backend()` is `"windows"` or `"none"`; a
machine with no desktop (this project's own test environment, a headless server)
gets `NoDesktop`, which refuses in plain language rather than half-working.

**This is a rewrite, not a port, and that is the risk it carries.** The original
drove a long-lived `powershell.exe -STA` process running a 449-line script of
P/Invoke primitives. Almost all of that file's value was its bug history, so
every one of those fixes is restated here as a requirement this module has to
meet, and each is exercised by `selfcheck.py` on a real Windows machine:

* **Focus before typing, always.** A synthetic click does not reliably move
  keyboard focus — Windows' anti-focus-stealing rules can suppress the side
  effect a real click would have — so text can land in the wrong window. Every
  action that types or presses a key calls `focus()` on its target first and
  never relies on a click having done it.
* **Close means `WM_CLOSE`, never killing a process.** Windows 11's Notepad can
  share one process across several windows, so terminating "the" process can
  close somebody else's unsaved document. pywinauto's `close()` posts the message
  the window's own X button posts, and the app is allowed to decline.
* **The cursor glides, and lands exactly on target.** An instant jump gives the
  user no chance to see where a click is about to happen; a ~250ms eased move
  does. It must END on the exact coordinate, because the mouse-drift stop guard
  compares the real cursor position against where we put it.
* **Scrolling goes both ways.** The original had to declare `mouse_event`'s
  `dwData` as `int` rather than the header's `uint` for a negative delta to
  survive; here the equivalent is simply never coercing the sign away.
* **Jarvis's own window is not a candidate.** The user is usually talking to
  Jarvis through a browser tab, so at the moment "look at my screen" runs, the
  foreground window is very often Jarvis itself.

Everything above is why `WindowsDesktop`'s methods are small and one-purpose:
each is a place a specific known failure has to be prevented, not a wrapper for
its own sake.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: How long a cursor move takes. Long enough to follow with your eyes, short
#: enough not to make a session feel padded.
GLIDE_SECONDS = 0.25

#: A window list longer than this is a runaway, not a workspace.
MAX_WINDOWS = 60

#: How much of a window's UI tree is worth reading. The model is shown these by
#: id, and a list past this length is noise it cannot act on anyway.
MAX_ELEMENTS = 120

_BROWSER_PROCESSES = frozenset({"chrome", "msedge", "firefox", "brave", "opera", "vivaldi"})


class NoDesktopHere(RuntimeError):
    """Raised by NoDesktop. Every caller turns this into a plain refusal."""


@dataclass(frozen=True)
class Window:
    handle: str
    title: str
    process_name: str
    foreground: bool = False
    rect: tuple[int, int, int, int] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"handle": self.handle, "title": self.title,
                "processName": self.process_name, "foreground": self.foreground,
                **({"rect": list(self.rect)} if self.rect else {})}


@dataclass(frozen=True)
class Element:
    id: int
    role: str
    name: str = ""
    value: str = ""
    #: Screen coordinates of the element's centre — where a click on it goes.
    center: tuple[int, int] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "role": self.role, "name": self.name,
                "value": self.value,
                **({"center": list(self.center)} if self.center else {})}


@dataclass(frozen=True)
class Shot:
    """A screen capture, as the bytes themselves — never a path.

    Whoever wants it saved decides where; a capture that writes a file as a side
    effect is one that cannot be taken without leaving something behind.
    """

    data: bytes
    mime_type: str = "image/png"
    width: int = 0
    height: int = 0


class Desktop(Protocol):
    """What the control loop, the screen tools and the monitor may ask for."""

    def windows(self) -> list[Window]: ...
    def processes(self) -> list[str]: ...
    def focus(self, handle: str) -> None: ...
    def read_window(self, handle: str) -> list[Element]: ...
    def screenshot(self, handle: str | None = None) -> Shot: ...
    def cursor(self) -> tuple[int, int]: ...
    def move_cursor(self, x: int, y: int) -> None: ...
    def click(self, x: int, y: int, *, button: str = "left", double: bool = False) -> None: ...
    def type_text(self, text: str) -> None: ...
    def key(self, combo: str) -> None: ...
    def scroll(self, amount: int) -> None: ...
    def launch(self, app: str) -> None: ...
    def minimize(self, handle: str) -> None: ...
    def restore(self, handle: str) -> None: ...
    def arrange(self, handle: str, x: int, y: int, width: int, height: int) -> None: ...
    def close(self, handle: str) -> None: ...


# --- the honest nothing ------------------------------------------------------

_NO_DESKTOP_MESSAGE = (
    "I can't see or control a screen on this computer — there's no Windows desktop "
    "for me to work with here."
)


@dataclass
class NoDesktop:
    """Every method refuses the same way. Not a stub with silent no-ops: a
    no-op would let a control session run to completion having done nothing,
    which is the fake success §45 exists to forbid."""

    reason: str = _NO_DESKTOP_MESSAGE

    def _refuse(self) -> Any:
        raise NoDesktopHere(self.reason)

    def windows(self) -> list[Window]: return self._refuse()
    def processes(self) -> list[str]: return self._refuse()
    def focus(self, handle: str) -> None: self._refuse()
    def read_window(self, handle: str) -> list[Element]: return self._refuse()
    def screenshot(self, handle: str | None = None) -> Shot: return self._refuse()
    def cursor(self) -> tuple[int, int]: return self._refuse()
    def move_cursor(self, x: int, y: int) -> None: self._refuse()
    def click(self, x: int, y: int, *, button: str = "left", double: bool = False) -> None:
        self._refuse()
    def type_text(self, text: str) -> None: self._refuse()
    def key(self, combo: str) -> None: self._refuse()
    def scroll(self, amount: int) -> None: self._refuse()
    def launch(self, app: str) -> None: self._refuse()
    def minimize(self, handle: str) -> None: self._refuse()
    def restore(self, handle: str) -> None: self._refuse()
    def arrange(self, handle: str, x: int, y: int, width: int, height: int) -> None:
        self._refuse()
    def close(self, handle: str) -> None: self._refuse()


# --- the real one ------------------------------------------------------------

class WindowsDesktop:
    """pywinauto for windows and their contents, pyautogui for input.

    Both are imported lazily, per method group, so that importing this module on
    a machine without them (every machine that is not the user's) costs nothing
    and fails nowhere.
    """

    def __init__(self) -> None:
        self._desktop: Any = None

    # -- lazily-held libraries ------------------------------------------------

    def _uia(self) -> Any:
        """pywinauto's UIA desktop. Held once: building it walks COM setup, and
        doing that per call would put a noticeable pause in front of every
        perceive step."""
        if self._desktop is None:
            from pywinauto import Desktop as UiaDesktop

            self._desktop = UiaDesktop(backend="uia")
        return self._desktop

    @staticmethod
    def _gui() -> Any:
        import pyautogui

        # A failsafe that aborts on a corner-of-screen cursor is the wrong
        # mechanism here: this build has three deliberate stops of its own, and
        # an exception thrown from inside a click leaves the loop unable to say
        # what happened.
        pyautogui.FAILSAFE = False
        return pyautogui

    # -- looking --------------------------------------------------------------

    def windows(self) -> list[Window]:
        import psutil

        found: list[Window] = []
        try:
            foreground_handle = self._foreground_handle()
        except Exception:  # noqa: BLE001 — a missing foreground is not a failure
            foreground_handle = None

        for element in self._uia().windows(visible_only=True, enabled_only=False):
            try:
                title = element.window_text() or ""
                handle = int(element.handle)
                pid = element.process_id()
            except Exception:  # noqa: BLE001 — a window can vanish mid-enumeration
                continue
            if not title.strip():
                continue                      # an untitled window is not addressable
            try:
                name = psutil.Process(pid).name().removesuffix(".exe")
            except Exception:  # noqa: BLE001
                name = ""
            rect = None
            try:
                box = element.rectangle()
                rect = (box.left, box.top, box.width(), box.height())
            except Exception:  # noqa: BLE001
                pass
            found.append(Window(handle=str(handle), title=title, process_name=name,
                                foreground=handle == foreground_handle, rect=rect))
            if len(found) >= MAX_WINDOWS:
                break
        return found

    @staticmethod
    def _foreground_handle() -> int:
        import ctypes

        return int(ctypes.windll.user32.GetForegroundWindow())

    def processes(self) -> list[str]:
        import psutil

        names = []
        for process in psutil.process_iter(["name"]):
            name = (process.info.get("name") or "").removesuffix(".exe").lower()
            if name and name not in names:
                names.append(name)
        return names

    def _window(self, handle: str) -> Any:
        from pywinauto import Application

        return Application(backend="uia").connect(handle=int(handle)).window(handle=int(handle))

    def read_window(self, handle: str) -> list[Element]:
        """The window's own control tree — real names, values and positions.

        More exact than a picture, and the reason clicking can be aimed at an
        element rather than at a coordinate a model guessed from an image.
        """
        elements: list[Element] = []
        window = self._window(handle)
        for index, child in enumerate(window.descendants()):
            if len(elements) >= MAX_ELEMENTS:
                break
            try:
                role = child.friendly_class_name()
                name = child.window_text() or ""
            except Exception:  # noqa: BLE001 — a control can disappear mid-walk
                continue
            value = ""
            try:
                if hasattr(child, "get_value"):
                    value = str(child.get_value() or "")
            except Exception:  # noqa: BLE001 — not every control has a value
                value = ""
            center = None
            try:
                box = child.rectangle()
                center = (box.mid_point().x, box.mid_point().y)
            except Exception:  # noqa: BLE001
                center = None
            if not name and not value:
                continue                      # nothing for the model to refer to
            elements.append(Element(id=index, role=role, name=name,
                                    value=value[:200], center=center))
        return elements

    def screenshot(self, handle: str | None = None) -> Shot:
        import io

        gui = self._gui()
        region = None
        if handle:
            try:
                box = self._window(handle).rectangle()
                region = (box.left, box.top, box.width(), box.height())
            except Exception:  # noqa: BLE001 — fall back to the whole screen
                region = None
        image = gui.screenshot(region=region)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return Shot(data=buffer.getvalue(), mime_type="image/png",
                    width=image.width, height=image.height)

    # -- acting ---------------------------------------------------------------

    def focus(self, handle: str) -> None:
        self._window(handle).set_focus()

    def cursor(self) -> tuple[int, int]:
        position = self._gui().position()
        return (int(position.x), int(position.y))

    def move_cursor(self, x: int, y: int) -> None:
        # Eased, and ending exactly on (x, y) — the drift guard compares the
        # real cursor against this coordinate to decide whether the user moved
        # the mouse themselves.
        self._gui().moveTo(x, y, duration=GLIDE_SECONDS)

    def click(self, x: int, y: int, *, button: str = "left", double: bool = False) -> None:
        gui = self._gui()
        self.move_cursor(x, y)
        if double:
            gui.doubleClick(x, y, button=button)
        else:
            gui.click(x, y, button=button)

    def type_text(self, text: str) -> None:
        # `write` sends real keystrokes; a long string is slow but keeps focus
        # semantics identical to a person typing, which the clipboard path does
        # not (a paste can be intercepted or disabled by the target app).
        self._gui().write(str(text), interval=0.01)

    def key(self, combo: str) -> None:
        parts = [p.strip().lower() for p in str(combo).replace("-", "+").split("+") if p.strip()]
        if not parts:
            return
        gui = self._gui()
        if len(parts) == 1:
            gui.press(parts[0])
        else:
            gui.hotkey(*parts)

    def scroll(self, amount: int) -> None:
        # The sign carries the direction. Never coerced to unsigned — the
        # original's own bug was exactly that, one layer down.
        self._gui().scroll(int(amount))

    def launch(self, app: str) -> None:
        """Start a program by name — through `open_app`'s own resolver, so the
        one place that decides what a name may resolve to stays one place."""
        from ..tools.open_app import launch_by_name

        launch_by_name(app)

    def minimize(self, handle: str) -> None:
        self._window(handle).minimize()

    def restore(self, handle: str) -> None:
        self._window(handle).restore()

    def arrange(self, handle: str, x: int, y: int, width: int, height: int) -> None:
        self._window(handle).move_window(x=int(x), y=int(y),
                                         width=int(width), height=int(height))

    def close(self, handle: str) -> None:
        # Posts WM_CLOSE, exactly like the window's own X. The app may refuse
        # (an unsaved-changes prompt), which is a normal outcome, not a failure.
        self._window(handle).close()


# --- which one, and how it is described --------------------------------------

_backend_cache: str | None = None
_desktop_cache: Any = None
_probe_detail: str = ""


def _probe() -> tuple[str, str]:
    """`(backend, detail)` — what is actually here, found by trying."""
    if sys.platform != "win32":
        return "none", f"this is {sys.platform}, not Windows"
    try:
        import pyautogui  # noqa: F401
        import pywinauto  # noqa: F401
    except Exception as err:  # noqa: BLE001
        return "none", f"the desktop libraries are not installed ({err})"
    try:
        import ctypes

        ctypes.windll.user32.GetForegroundWindow()
    except Exception as err:  # noqa: BLE001
        return "none", f"the window system did not answer ({err})"
    return "windows", ""


def backend() -> str:
    """'windows' or 'none'. Probed once — the answer cannot change while the
    process runs, and probing per call would put COM setup in front of every
    action."""
    global _backend_cache, _probe_detail
    if _backend_cache is None:
        _backend_cache, _probe_detail = _probe()
    return _backend_cache


def available() -> bool:
    return backend() == "windows"


def get_desktop() -> Desktop:
    global _desktop_cache
    if _desktop_cache is None:
        _desktop_cache = WindowsDesktop() if available() else NoDesktop(describe_desktop())
    return _desktop_cache


def describe_desktop() -> str:
    """The sentence a tool's own description and refusal carry. Written from
    what the probe found, and it says what is NOT possible."""
    if available():
        return ("I can see the windows on this computer, read what's in them, and use "
                "the mouse and keyboard.")
    backend()                                  # ensure the detail is populated
    return f"{_NO_DESKTOP_MESSAGE} ({_probe_detail})." if _probe_detail else _NO_DESKTOP_MESSAGE


def is_jarvis_own_window(window: Window) -> bool:
    """Jarvis's own browser tab, which must never be the thing it looks at.

    Matched on a known browser process AND the page's real title — not a bare
    process check, so a File Explorer window that happens to be called
    "Jarvis-001" is never wrongly excluded.
    """
    if (window.process_name or "").lower() not in _BROWSER_PROCESSES:
        return False
    title = (window.title or "").strip()
    return title == "Jarvis" or title.startswith(("Jarvis -", "Jarvis –", "Jarvis —"))


_WHOLE_SCREEN = frozenset({"", "screen", "desktop", "everything", "my screen", "whole screen"})


def pick_window(windows: list[Window], target: str | None = None) -> Window | None:
    """The best match for a free-text target, or the front one.

    Pure, so it is testable with no desktop at all — which is most of what makes
    the targeting rules verifiable off a Windows machine.
    """
    others = [w for w in windows if not is_jarvis_own_window(w)]
    # Only fall back to including Jarvis's own window if it is genuinely the
    # only one open: looking at nothing is a worse answer than looking at us.
    pool = others or windows
    cleaned = str(target or "").strip().lower()
    if cleaned in _WHOLE_SCREEN:
        # The list is z-ordered, so once our own tab is gone the first entry is
        # a good proxy for "whatever they were just looking at" even when focus
        # is sitting in Jarvis's own text box.
        return next((w for w in pool if w.foreground), None) or (pool[0] if pool else None)
    match = next((w for w in pool
                  if cleaned in (w.title or "").lower()
                  or cleaned in (w.process_name or "").lower()), None)
    return match or next((w for w in pool if w.foreground), None) or (pool[0] if pool else None)


def set_desktop_for_tests(desktop: Any) -> None:
    """Test-only: install a fake desktop, so the loop, the tools and the monitor
    are exercised for real on a machine that has no screen at all."""
    global _desktop_cache, _backend_cache
    _desktop_cache = desktop
    _backend_cache = "windows" if desktop is not None and not isinstance(desktop, NoDesktop) else None


def reset_for_tests() -> None:
    global _backend_cache, _desktop_cache, _probe_detail
    _backend_cache = None
    _desktop_cache = None
    _probe_detail = ""
