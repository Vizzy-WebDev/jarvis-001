"""Operating the computer: the screen, the windows, the mouse and the keyboard.

Nothing here imports the tool loader, the capability registry or the
orchestrator — a control session's own action list is its own, never the chat
catalogue, and the import graph is what keeps that true rather than a comment.
"""

from .desktop import (
    Desktop, Element, NoDesktopHere, Shot, Window, available, backend,
    describe_desktop, get_desktop, is_jarvis_own_window, pick_window,
)

__all__ = ["Desktop", "Element", "NoDesktopHere", "Shot", "Window", "available",
           "backend", "describe_desktop", "get_desktop", "is_jarvis_own_window",
           "pick_window"]
