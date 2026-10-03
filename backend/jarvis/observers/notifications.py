"""Writing down what subsystems announce.

The scheduler, the monitor, the heartbeat and Memory all publish
`NOTIFICATION_CREATED` and nothing was listening, so a task that failed
overnight told an empty room. This subscribes, exactly like every other
recorder here — nothing had to be changed in any of those subsystems, which is
the point of the event seam.
"""

from __future__ import annotations

import logging

from ..events.bus import Event

logger = logging.getLogger(__name__)


def store_notification(event: Event) -> None:
    from .. import notifications

    payload = event.payload or {}
    title = str(payload.get("title") or "").strip()
    if not title:
        # A notice with nothing to say is not one. Logged rather than dropped
        # silently: it means a publisher is wrong.
        logger.info("a notification was published with no title: %s", payload)
        return
    notifications.add(
        kind=str(payload.get("kind") or "system"),
        level=str(payload.get("level") or "info"),
        title=title,
        body=str(payload.get("body") or ""),
        action=payload.get("action") if isinstance(payload.get("action"), dict) else None,
        meta=payload.get("meta") if isinstance(payload.get("meta"), dict) else None,
    )
