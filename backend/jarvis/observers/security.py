"""Counting the things that ought to be rare, so a burst of them is visible.

Nothing here decides anything — it keeps a tally the security checks read. The
alternative is a check that has to reconstruct history from the trace, which
would make "how often does this normally happen" a query rather than a fact.
"""

from __future__ import annotations

import logging

from ..events import EventType
from ..events.bus import Event

logger = logging.getLogger(__name__)


def count_event(event: Event) -> None:
    from ..ops.diagnostics.checks.security import counters

    if event.type is EventType.TOOL_FAILED:
        counters.record("tool_failed")
        return
    if event.type is EventType.APPROVAL_RESOLVED and not event.payload.get("approved", True):
        counters.record("approval_denied")
