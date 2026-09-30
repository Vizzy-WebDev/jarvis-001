"""Discovery: ask each connection what it serves. Runs at startup and on
`refresh_catalog()` — never on the request path.

A listing that works replaces the last one in the state file. One that fails
keeps the last known listing (config's own models never depend on it), and a
connection that couldn't be reached at all rests for a while so calls don't queue
up behind it. Nothing here stops the others loading.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from . import config, drivers, state
from .errors import ModelError
from .prepared import ConnInfo

logger = logging.getLogger(__name__)

_UNREACHABLE = ("unavailable", "timeout")


def conn_info(conn: Any, cfg: config.Config) -> ConnInfo:
    from .. import config as secrets

    key = secrets.get_secret(conn.secret_ref) if conn.secret_ref else None
    return ConnInfo(name=conn.name, base_url=conn.base_url, api_key=key, quirks=dict(cfg.quirks_of(conn).wire))


def refresh_one(name: str, cfg: config.Config | None = None) -> dict[str, Any]:
    cfg = cfg or config.current()
    conn = cfg.connections.get(name)
    if conn is None:
        return {"ok": False, "message": f"There's no connection called “{name}”."}
    try:
        found = drivers.get(conn.driver).discover(conn_info(conn, cfg))
    except ModelError as err:
        state.record_discovery(name, None, str(err))
        if err.type in _UNREACHABLE:
            state.mark_connection_down(name, str(err), cfg.settings.unreachable_rest_s)
        return {"ok": False, "message": str(err), "error": err.type}
    except Exception as err:  # noqa: BLE001 - one bad server must not stop the rest loading
        logger.exception("discovery on %s failed unexpectedly", name)
        state.record_discovery(name, None, f"Jarvis couldn't read what it listed: {err}")
        return {"ok": False, "message": "Jarvis couldn't read the list of models it sent.", "error": "unavailable"}
    state.record_discovery(name, found)
    state.mark_connection_up(name)
    count = len(found)
    return {"ok": True, "models": count,
            "message": f"Connected. {count} model{'s' if count != 1 else ''} listed."}


def refresh(name: str | None = None) -> dict[str, dict[str, Any]]:
    """One connection, or every connection that has discovery switched on."""
    cfg = config.current()
    if name is not None:
        return {name: refresh_one(name, cfg)}
    return {n: refresh_one(n, cfg) for n, c in cfg.connections.items() if c.discovery}


def refresh_in_background() -> threading.Thread:
    """Startup discovery, off the request path."""
    def run() -> None:
        try:
            refresh()
        except Exception:  # noqa: BLE001 - a broken config is reported where it's used
            logger.exception("startup model discovery failed")

    thread = threading.Thread(target=run, name="model-discovery", daemon=True)
    thread.start()
    return thread
