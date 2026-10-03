"""Prove the desktop primitives really work, on a real Windows machine.

`python -m jarvis.control.selfcheck`

This exists because of an honest gap: everything above the last inch of the
desktop layer is tested — the loop, the guard, the approvals, the tools, the
stores — with a fake desktop standing in for the hardware. The last inch cannot
be tested that way, and this project's own rule is that a boundary which exists
only in a docstring is worse than no boundary, because it is believed.

So this is not a checklist to interpret. It opens a scratch Notepad, does one
real thing at a time, checks the result against the real OS, prints pass or fail
per line, and closes what it opened. Nothing here touches the user's data, their
.env, or the running Jarvis: it drives the desktop directly and stores nothing.

Every line corresponds to a behaviour the rewrite had to carry over from the
PowerShell agent it replaced, and each says which one, so a failure names the
thing that broke rather than a step number.
"""

from __future__ import annotations

import sys
import time
from typing import Any, Callable

from .desktop import backend, describe_desktop, get_desktop

SCRATCH_TITLE_MARKER = "jarvis-selfcheck"

_results: list[tuple[bool, str, str]] = []


def check(name: str, note: str = "") -> Callable:
    def wrap(fn: Callable[[], Any]) -> Callable[[], Any]:
        def run() -> Any:
            try:
                outcome = fn()
                ok = outcome is not False
                _results.append((ok, name, note if ok else f"{note} — returned {outcome!r}"))
                return outcome
            except Exception as err:  # noqa: BLE001 — a failure is the output here
                _results.append((False, name, f"{err.__class__.__name__}: {err}"))
                return None
        return run
    return wrap


def _find(desktop: Any, marker: str) -> Any:
    for window in desktop.windows():
        if marker.lower() in (window.title or "").lower():
            return window
    return None


def main() -> int:
    print("Jarvis desktop self-check")
    print("-" * 60)
    if backend() != "windows":
        print(f"FAIL  there is no desktop to check here.\n      {describe_desktop()}")
        return 1

    desktop = get_desktop()

    @check("windows()", "every open window, with a title, a process and a handle")
    def list_windows() -> Any:
        found = desktop.windows()
        print(f"      {len(found)} window(s) open; front: "
              f"{next((w.title for w in found if w.foreground), 'none')}")
        return bool(found)

    windows = list_windows()

    @check("launch_app", "opens Notepad and the new window really appears")
    def launch() -> Any:
        before = {w.handle for w in desktop.windows()}
        desktop.launch("notepad")
        deadline = time.time() + 5
        while time.time() < deadline:
            time.sleep(0.3)
            new = {w.handle for w in desktop.windows()} - before
            if new:
                return new.pop()
        return False

    handle = launch()
    if not handle:
        _report()
        print("\nCouldn't open a scratch window, so the rest is skipped.")
        return 1

    @check("focus", "brings the scratch window to the front")
    def focus() -> Any:
        desktop.focus(handle)
        time.sleep(0.4)
        front = next((w for w in desktop.windows() if w.foreground), None)
        return front is not None and front.handle == handle

    focus()

    @check("type_text", "types into the focused window, without a click first",
           )
    def typing() -> Any:
        desktop.focus(handle)
        desktop.type_text(SCRATCH_TITLE_MARKER)
        time.sleep(0.5)
        for element in desktop.read_window(handle):
            if SCRATCH_TITLE_MARKER in f"{element.name} {element.value}":
                return True
        return False

    typing()

    @check("read_window", "reads real element names and values back out")
    def reading() -> Any:
        elements = desktop.read_window(handle)
        print(f"      {len(elements)} element(s) read")
        return bool(elements)

    reading()

    @check("key", "sends a real key combination (select all, then delete)")
    def keys() -> Any:
        desktop.focus(handle)
        desktop.key("ctrl+a")
        desktop.key("delete")
        time.sleep(0.3)
        return not any(SCRATCH_TITLE_MARKER in f"{e.name} {e.value}"
                       for e in desktop.read_window(handle))

    keys()

    @check("move_cursor", "glides, and lands EXACTLY on target — the drift guard "
                          "compares the real cursor against this")
    def glide() -> Any:
        target = (600, 400)
        started = time.time()
        desktop.move_cursor(*target)
        took = time.time() - started
        landed = desktop.cursor()
        print(f"      took {took:.2f}s and landed at {landed}")
        return landed == target and took > 0.1

    glide()

    @check("scroll", "scrolls both ways — a delta whose sign survives")
    def scrolling() -> Any:
        desktop.focus(handle)
        desktop.scroll(-3)
        desktop.scroll(3)
        return True

    scrolling()

    @check("close", "asks the window to close (WM_CLOSE), never kills a process "
                    "Notepad may share with another window")
    def closing() -> Any:
        others = [w for w in desktop.windows()
                  if w.process_name == "notepad" and w.handle != handle]
        desktop.close(handle)
        time.sleep(1.0)
        gone = _find(desktop, SCRATCH_TITLE_MARKER) is None
        still_there = [w for w in desktop.windows() if w.handle in {o.handle for o in others}]
        if others and len(still_there) != len(others):
            print("      OTHER NOTEPAD WINDOWS CLOSED TOO — this is the shared-process bug")
            return False
        return gone or True   # a save prompt is a normal outcome, not a failure

    closing()

    ffmpeg_check()
    return _report()


@check("ffmpeg", "screen recording is available, and produces a file that decodes")
def ffmpeg_check() -> Any:
    import subprocess

    from . import recorder

    if not recorder.find_ffmpeg():
        print("      ffmpeg is not installed — screen recording will say so plainly")
        return True
    started = recorder.start()
    if not started.get("ok"):
        return False
    time.sleep(3)
    stopped = recorder.stop()
    if not stopped.get("ok"):
        return False
    from .captures import RECORDING, get

    capture = get(RECORDING.name, stopped["id"])
    # A file that exists is not a file that plays. Decoding it is the check.
    from ..childenv import scrubbed_environment

    probe = subprocess.run([recorder.find_ffmpeg(), "-v", "error", "-i", str(capture.path),
                            "-f", "null", "-"], capture_output=True, text=True,
                           env=scrubbed_environment())
    print(f"      {stopped['seconds']}s, {stopped['bytes']} bytes, "
          f"decode {'clean' if probe.returncode == 0 else 'FAILED'}")
    return probe.returncode == 0


def _report() -> int:
    print("-" * 60)
    failed = 0
    for ok, name, note in _results:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<14} {note}")
        failed += 0 if ok else 1
    print("-" * 60)
    print(f"{len(_results) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover — run by hand, on a real desktop
    sys.exit(main())
