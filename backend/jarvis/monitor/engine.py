"""Evaluating watches, cheapest condition first.

**Only the conditions that genuinely work here are offered**, and that is now a
question about the machine rather than about this build. A file appearing, a
file finishing (its size stops changing between readings) and a page changing
work everywhere. Watching a window, a process or the screen needs a desktop, so
those are refused where there isn't one — with a plain reason, rather than
accepted into a watch that would silently never fire. A watch that cannot fire
is worse than a refusal, because the user believes it is running.

Cheapest first, and only one kind costs a model call: `screen_looks_like` asks
whether a picture matches a description, which nothing else here can answer. It
is deliberately last in the file and last in the mind — a watch that runs every
few minutes and spends a model call each time is a different kind of thing from
one that stats a file.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from ..events import EventType, bus as default_bus
from ..events.bus import EventBus
from ..jscompat import now_iso
from . import store

logger = logging.getLogger(__name__)

SUPPORTED_KINDS = ("file_exists", "file_gone", "file_size_stable", "web_page_changed")

#: The kinds that need a real desktop. Named individually so a refusal can say
#: which, rather than "unsupported".
DESKTOP_KINDS = ("window_appears", "window_gone", "window_title_matches",
                 "process_running", "process_gone", "element_text_matches",
                 "screen_looks_like")


class UnsupportedCheck(ValueError):
    pass


def validate(check: dict[str, Any]) -> None:
    kind = (check or {}).get("kind")
    if kind in SUPPORTED_KINDS:
        return
    if kind in DESKTOP_KINDS:
        from ..control import available, describe_desktop

        if available():
            return
        raise UnsupportedCheck(
            f"I can't watch for that ({kind}) — {describe_desktop()} I can watch for a file "
            "appearing or finishing, or a web page changing.")
    raise UnsupportedCheck(f"I don't know how to watch for {kind!r}.")


def evaluate(monitor: dict[str, Any]) -> tuple[bool, Any]:
    """(triggered, new state). State is carried between checks — a file being
    "finished" is only knowable by comparing two readings."""
    check = monitor.get("check") or {}
    kind = check.get("kind")

    if kind == "file_exists":
        return Path(check.get("path", "")).exists(), None
    if kind == "file_gone":
        return not Path(check.get("path", "")).exists(), None

    if kind == "file_size_stable":
        path = Path(check.get("path", ""))
        if not path.exists():
            return False, None
        size = path.stat().st_size
        previous = monitor.get("state")
        # Two equal readings in a row, and never on the first: one reading
        # cannot tell "finished" from "not started".
        return (previous is not None and previous == size), size

    if kind in DESKTOP_KINDS:
        return _evaluate_desktop(monitor, check, kind)

    if kind == "web_page_changed":
        from ..tools._http import get_text

        try:
            digest = hashlib.sha256(get_text(check.get("url", "")).encode()).hexdigest()
        except Exception as err:  # noqa: BLE001
            logger.info("could not read %s: %s", check.get("url"), err)
            return False, monitor.get("state")
        previous = monitor.get("state")
        return (previous is not None and previous != digest), digest

    return False, monitor.get("state")


def _evaluate_desktop(monitor: dict[str, Any], check: dict[str, Any],
                      kind: str) -> tuple[bool, Any]:
    """The kinds that need a screen. Every one reads live state and compares it
    to what was asked for; only the last spends a model call."""
    from ..control import get_desktop, pick_window

    desktop = get_desktop()
    wanted = str(check.get("target") or check.get("text") or check.get("description") or "")

    if kind in ("process_running", "process_gone"):
        running = {name.lower() for name in desktop.processes()}
        # Substring, not equality: people say "chrome", not "chrome.exe", and a
        # watch that needs the exact executable name is a watch that never fires.
        present = any(wanted.lower() in name for name in running if wanted)
        return (present if kind == "process_running" else not present), None

    windows = desktop.windows()
    if kind in ("window_appears", "window_gone"):
        found = any(wanted.lower() in f"{w.title} {w.process_name}".lower()
                    for w in windows if wanted)
        return (found if kind == "window_appears" else not found), None

    if kind == "window_title_matches":
        return any(wanted.lower() in (w.title or "").lower() for w in windows if wanted), None

    if kind == "element_text_matches":
        window = pick_window(windows, str(check.get("window") or ""))
        if window is None:
            return False, None
        for element in desktop.read_window(window.handle):
            if wanted and wanted.lower() in f"{element.name} {element.value}".lower():
                return True, None
        return False, None

    if kind == "screen_looks_like":
        return _screen_looks_like(monitor, wanted, str(check.get("window") or ""))

    return False, monitor.get("state")


def _screen_looks_like(monitor: dict[str, Any], description: str,
                       target: str) -> tuple[bool, Any]:
    """The one condition that costs a model call — asked as a yes/no question.

    The badge is lit while it looks, exactly as a one-off glance lights it: a
    watch is a reason to see the screen, not an exemption from saying so.
    """
    import base64

    from ..ai import ask_model
    from ..control import get_desktop, pick_window
    from ..control.watching import observe

    if not description:
        return False, None
    desktop = get_desktop()
    with observe("Jarvis is watching your screen"):
        window = pick_window(desktop.windows(), target)
        shot = desktop.screenshot(window.handle if window else None)
        reply = ask_model(
            f"Does this screen show the following, right now?\n\n{description}\n\n"
            'Answer with JSON: {"matches": true or false}. Say false unless it is '
            "clearly the case.",
            media=[{"kind": "image", "mimeType": shot.mime_type,
                    "dataBase64": base64.b64encode(shot.data).decode("ascii")}],
            need={"vision": True}, want_json=True)
    if not reply.ok or not isinstance(reply.data, dict):
        # No model, or an answer that could not be read: not a trigger. A watch
        # that fires because it could not see is worse than one that waits.
        return False, monitor.get("state")
    return bool(reply.data.get("matches")), monitor.get("state")


def check_all(event_bus: EventBus | None = None) -> list[dict[str, Any]]:
    """One pass over every active watch. Returns what triggered."""
    ebus = event_bus or default_bus
    fired: list[dict[str, Any]] = []

    for monitor in store.list_watching():
        try:
            triggered, state = evaluate(monitor)
        except Exception as err:  # noqa: BLE001 — one bad watch must not stop the others
            logger.exception("monitor %s failed", monitor["id"])
            store.update_monitor(monitor["id"], {"error": str(err), "lastCheckedAt": now_iso()})
            continue

        store.update_monitor(monitor["id"], {"lastCheckedAt": now_iso(), "state": state})
        if not triggered:
            continue

        store.update_monitor(monitor["id"], {"status": "triggered", "triggeredAt": now_iso()})
        fired.append(monitor)
        ebus.publish(EventType.NOTIFICATION_CREATED, {
            "kind": "monitor", "level": "info",
            "title": f"What you asked me to watch for happened: {monitor['description']}",
            "body": (monitor.get("onTrigger") or {}).get("text") or "",
            "meta": {"monitorId": monitor["id"]}})
    return fired
