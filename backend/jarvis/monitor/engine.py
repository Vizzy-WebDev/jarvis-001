"""Evaluating watches, cheapest condition first.

**Only the conditions that genuinely work here are offered.** Watching a window
or a process needs the desktop bridge, which is not built yet — so those kinds
are REFUSED at creation with a plain reason rather than accepted into a watch
that would silently never fire. A watch that cannot fire is worse than a refusal,
because the user believes it is running.

Available now: a file appearing, a file finishing being written (its size stops
changing between checks), and a web page changing. Each is a cheap read and none
needs a model call to evaluate.
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

#: Kinds the original supports that need the desktop bridge. Named individually
#: so the refusal can say which, rather than "unsupported".
DEFERRED_KINDS = ("window_appears", "window_gone", "window_title_matches",
                  "process_running", "process_gone", "element_text_matches",
                  "screen_looks_like")


class UnsupportedCheck(ValueError):
    pass


def validate(check: dict[str, Any]) -> None:
    kind = (check or {}).get("kind")
    if kind in SUPPORTED_KINDS:
        return
    if kind in DEFERRED_KINDS:
        raise UnsupportedCheck(
            f"I can't watch for that yet ({kind}) — watching windows, processes and the "
            "screen needs the desktop side, which isn't wired up in this version. I can "
            "watch for a file appearing or finishing, or a web page changing.")
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
