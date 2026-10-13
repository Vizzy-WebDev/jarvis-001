"""Which browser on this computer Jarvis should drive.

Jarvis does not download a browser. It uses Chrome or Edge, one of which every
Windows 11 machine has, started through Playwright with an explicit path. The
older lookup (`webrender.browser_path`) only searched PATH, where Windows
browsers are not, so on a normal Windows install it found nothing and the
browser connector answered "no browser installed".

Order: an explicit override, whatever the older lookup finds, Chrome, Edge,
Chromium, and finally the Chromium Playwright itself may have downloaded.
"""

from __future__ import annotations

import glob
import os
import sys
from pathlib import Path

from ..webrender import browser_path as _on_path

OVERRIDE_ENV = "JARVIS_BROWSER_PATH"


def _windows_candidates() -> list[str]:
    roots = [os.environ.get(name) for name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
    parts = [r"Google\Chrome\Application\chrome.exe", r"Microsoft\Edge\Application\msedge.exe",
             r"Chromium\Application\chrome.exe"]
    found = [str(Path(root) / part) for part in parts for root in roots if root]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        found += sorted(glob.glob(os.path.join(local, "ms-playwright", "chromium-*",
                                               "chrome-win*", "chrome.exe")), reverse=True)
    return found


def _mac_candidates() -> list[str]:
    apps = ["Google Chrome.app/Contents/MacOS/Google Chrome",
            "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "Chromium.app/Contents/MacOS/Chromium"]
    return [f"/Applications/{app}" for app in apps]


def _linux_candidates() -> list[str]:
    found = ["/usr/bin/google-chrome", "/usr/bin/microsoft-edge", "/usr/bin/chromium",
             "/usr/bin/chromium-browser"]
    found += sorted(glob.glob(str(Path.home() / ".cache" / "ms-playwright" / "chromium-*"
                                  / "chrome-linux" / "chrome")), reverse=True)
    return found


def find_browser() -> str | None:
    override = os.environ.get(OVERRIDE_ENV)
    if override and os.path.isfile(override):
        return override
    if sys.platform == "win32":
        candidates = _windows_candidates()
    elif sys.platform == "darwin":
        candidates = _mac_candidates()
    else:
        candidates = _linux_candidates()
    on_path = _on_path()
    # `browser_path` may hand back a folder (a packaged Chromium directory); only a file runs.
    if on_path and os.path.isfile(on_path):
        return on_path
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None
