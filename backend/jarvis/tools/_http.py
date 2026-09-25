"""Shared HTTP + platform helpers for the built-in tools.

Deliberately tiny and dependency-free beyond httpx, which the app already needs.
Kept out of any tool file so no tool has to import another.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from typing import Any

import httpx

TIMEOUT_S = 15.0
USER_AGENT = "Jarvis/1.0 (personal assistant)"


def get_json(url: str, **kw: Any) -> Any:
    with httpx.Client(timeout=TIMEOUT_S, follow_redirects=True,
                      headers={"User-Agent": USER_AGENT}) as client:
        response = client.get(url, **kw)
        response.raise_for_status()
        return response.json()


def get_text(url: str, **kw: Any) -> str:
    with httpx.Client(timeout=TIMEOUT_S, follow_redirects=True,
                      headers={"User-Agent": USER_AGENT}) as client:
        response = client.get(url, **kw)
        response.raise_for_status()
        return response.text


def open_in_browser(url: str) -> bool:
    """Hand a URL to the desktop's own handler. False when there is no desktop.

    Returns rather than raises so a tool can report honestly that it produced a
    link but could not open it — which is exactly what happens on a headless
    machine, and pretending otherwise would be a fake success (§45).
    """
    system = platform.system()
    try:
        if system == "Windows":
            os.startfile(url)  # type: ignore[attr-defined]  # noqa: S606 — no shell
            return True
        opener = "open" if system == "Darwin" else "xdg-open"
        if not shutil.which(opener):
            return False
        # The URL is a single argv entry, never interpolated into a shell string.
        subprocess.Popen([opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:  # noqa: BLE001
        return False
