"""The red bar that says Jarvis is driving, and the two stops that live outside it.

**Its own process, deliberately.** A control session spends its time focusing
other people's windows; a stop button drawn by the same process that is busy
clicking is a stop button that can be blocked by exactly the thing you need to
stop. A separate process keeps drawing and keeps responding.

Built with `tkinter` from the standard library rather than the PowerShell WinForms
window the original spawned. Same properties — always on top, small, unmissable —
with no second language in the build and no compile step that can fail on a
machine where the assemblies were not referenced.

Three stops, and this file owns two of them:

* the button here,
* the global hotkey (Ctrl+Alt+J), polled rather than registered — a registered
  hotkey can be refused if something else already owns the combination, and
  failing silently is the one thing a stop must never do,
* and moving your own mouse, which lives in the session itself because that is
  where the cursor is compared.

None of them pauses. A person reaching for their mouse means "that's enough".
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

HOTKEY_LABEL = "Ctrl+Alt+J"
#: Virtual-key codes: control, menu (alt), and J.
_VK_CONTROL, _VK_MENU, _VK_J = 0x11, 0x12, 0x4A
_POLL_SECONDS = 0.15


# --- the window (child process) ---------------------------------------------

def _draw(goal: str, stop_url: str) -> None:
    """The overlay itself. Runs in the child; never imported by the server."""
    import tkinter as tk
    import urllib.request

    root = tk.Tk()
    root.title("Jarvis is using your computer")
    root.attributes("-topmost", True)
    root.overrideredirect(True)
    root.configure(bg="#b3261e")
    width, height = 460, 44
    screen_width = root.winfo_screenwidth()
    root.geometry(f"{width}x{height}+{(screen_width - width) // 2}+12")

    label = tk.Label(root, text=f"Jarvis is using your computer — {goal[:60]}",
                     bg="#b3261e", fg="white", font=("Segoe UI", 10))
    label.pack(side="left", padx=12)

    def stop() -> None:
        try:
            urllib.request.urlopen(stop_url, data=b"{}", timeout=3)
        except Exception as err:  # noqa: BLE001 — the window must close regardless
            logger.info("overlay could not reach the server: %s", err)
        root.destroy()

    tk.Button(root, text=f"Stop  ({HOTKEY_LABEL})", command=stop,
              bg="white", fg="#b3261e", relief="flat",
              font=("Segoe UI", 9, "bold")).pack(side="right", padx=10, pady=8)
    root.mainloop()


class Overlay:
    """A handle on the child process. `show()` never raises: an overlay that
    cannot be drawn must not take the session down with it — the hotkey and the
    mouse guard are still there, and the session is still stoppable."""

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None

    def show(self, goal: str, stop_url: str) -> bool:
        if self._process is not None:
            return True
        from ..childenv import scrubbed_environment

        try:
            self._process = subprocess.Popen(
                [sys.executable, "-m", "jarvis.control.overlay", goal, stop_url],
                # The overlay draws a window; it has no business holding the
                # user's API keys.
                env=scrubbed_environment(),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception as err:  # noqa: BLE001
            logger.info("no control overlay this time: %s", err)
            self._process = None
            return False

    def hide(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        try:
            process.terminate()
            process.wait(timeout=3)
        except Exception:  # noqa: BLE001 — a window that will not close is not worth an error
            pass

    @property
    def showing(self) -> bool:
        return self._process is not None and self._process.poll() is None


# --- the hotkey (server process) ---------------------------------------------

def hotkey_pressed() -> bool:
    """Whether Ctrl+Alt+J is held right now.

    Polled with `GetAsyncKeyState` rather than registered with `RegisterHotKey`:
    registration can be refused when another program already owns the
    combination, and it fails by simply never firing — which, for the control
    the user reaches for when something is going wrong, is the worst possible
    failure mode.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        held = 0x8000
        return all(user32.GetAsyncKeyState(key) & held
                   for key in (_VK_CONTROL, _VK_MENU, _VK_J))
    except Exception:  # noqa: BLE001
        return False


def watch_for_hotkey(stop_now: Callable[[], Any], is_running: Callable[[], bool],
                     *, poll_seconds: float = _POLL_SECONDS,
                     pressed: Callable[[], bool] | None = None) -> threading.Thread:
    """Watch until the session ends. Injectable so the loop itself is testable
    on a machine with no keyboard to hold down."""
    check = pressed or hotkey_pressed

    def loop() -> None:
        while is_running():
            try:
                if check():
                    stop_now()
                    return
            except Exception:  # noqa: BLE001 — a broken poll must not end the session
                logger.debug("hotkey poll failed", exc_info=True)
            time.sleep(poll_seconds)

    thread = threading.Thread(target=loop, name="control-hotkey", daemon=True)
    thread.start()
    return thread


def stop_url() -> str:
    """Where the overlay sends its click. The server binds 127.0.0.1 only, so
    this is the same address the browser uses."""
    port = os.environ.get("PORT") or "3000"
    return f"http://127.0.0.1:{port}/api/control/stop"


if __name__ == "__main__":  # pragma: no cover — the child process
    _draw(sys.argv[1] if len(sys.argv) > 1 else "",
          sys.argv[2] if len(sys.argv) > 2 else stop_url())
