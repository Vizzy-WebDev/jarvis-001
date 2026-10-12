"""Keeps each connected app's cached tool list in step with the app itself.

A connector's tool list is read when it connects and only again when someone presses Refresh, so
every tool an app added afterwards stayed invisible to Jarvis (live, Oct 2026: Higgsfield 156 tools
against 101 known). This sweep re-reads any working app's list once it is older than
`STALE_AFTER_S`, on its own clock behind its own interlock like every other background subsystem
(see `icons.py`, whose shape this mirrors).

Deliberate limits:
- **Only apps that work.** An app that is broken or switched off is left for the person; a sweep
  cannot fix a refused sign-in and must not hammer a server that is refusing it.
- **A bad moment is not a verdict.** A timeout or an unreachable server is logged and retried next
  sweep; it does NOT change a working app's status. (A refused sign-in still marks itself — that
  is `mcp_client.AuthRejected`, which is true news.)
- **One app at a time**, so a slow server delays only the sweep, never the person.
- Reading a list is the only thing it does. Tools it finds are offered to the person under the
  ordinary default, **Ask** — a new tool does not run unasked because it appeared on its own.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any

from . import store

logger = logging.getLogger(__name__)

ENABLE_ENV = "JARVIS_CONNECTOR_TOOLS"
#: How old a list may be before it is read again.
STALE_AFTER_S = 24 * 60 * 60
#: How often the sweep looks. Much shorter than STALE_AFTER_S so an app is re-read soon after its
#: list goes stale rather than up to a full day later.
SWEEP_INTERVAL_S = 2 * 60 * 60
#: Wait before the first sweep, so a launch is not also making network calls while it starts.
FIRST_SWEEP_DELAY_S = 45

_timer: threading.Timer | None = None


def _age_s(stamp: Any) -> float | None:
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - parsed).total_seconds()


def is_stale(connector: dict[str, Any]) -> bool:
    """Whether this connector should be re-read now."""
    if connector.get("type") not in ("mcp", "api") or not connector.get("enabled", True):
        return False
    if (connector.get("status") or {}).get("state") != "working":
        return False
    config = connector.get("config") or {}
    if connector["type"] == "api" and not config.get("specUrl"):
        return False  # an API connector's operations are its own, there is no server list to read
    age = _age_s(config.get("toolsRefreshedAt"))
    return age is None or age > STALE_AFTER_S


def refresh_stale() -> dict[str, list]:
    """Re-read every outdated list. Returns what happened, for the log and the tests."""
    from . import capabilities

    report: dict[str, list] = {"refreshed": [], "failed": []}
    changed = False
    for connector in store.list_connectors():
        if not is_stale(connector):
            continue
        before = {t.get("name") for t in (connector.get("config") or {}).get("tools") or []}
        try:
            result = capabilities.refresh_tools(connector["id"])
        except Exception as err:  # noqa: BLE001 — one app failing must not stop the rest
            logger.info("connector %s: refreshing its tools failed, will retry: %s",
                        connector.get("label"), err)
            report["failed"].append({"id": connector["id"], "error": str(err)})
            continue
        report["refreshed"].append(connector["id"])
        changed = True
        after = {t.get("name") for t in (result["connector"].get("config") or {}).get("tools") or []}
        if after != before and before:
            logger.info("connector %s: tools changed (+%d, -%d)", connector.get("label"),
                        len(after - before), len(before - after))
    if changed:
        try:
            from ..assembly import get_registry

            capabilities.sync(get_registry())
        except Exception:  # noqa: BLE001 — the lists are saved; the next declaration build reads them
            logger.exception("registering refreshed connector tools failed")
    return report


def is_enabled() -> bool:
    return os.environ.get(ENABLE_ENV) == "1"


def start() -> bool:
    global _timer
    if not is_enabled() or _timer is not None:
        return False

    def run() -> None:
        global _timer
        try:
            refresh_stale()
        except Exception:  # noqa: BLE001 — a bad sweep must not break the process
            logger.exception("refreshing connector tool lists failed")
        _timer = threading.Timer(SWEEP_INTERVAL_S, run)
        _timer.daemon = True
        _timer.start()

    _timer = threading.Timer(FIRST_SWEEP_DELAY_S, run)
    _timer.daemon = True
    _timer.start()
    return True


def stop() -> None:
    global _timer
    if _timer is not None:
        _timer.cancel()
        _timer = None
