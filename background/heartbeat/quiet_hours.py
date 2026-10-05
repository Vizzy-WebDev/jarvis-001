"""When Jarvis must not speak first.

Gates LIVE SPEECH only. The notification and the outbox row a finding produces
are written regardless — they take effect once the user is already engaging, at
which point quiet hours has nothing left to protect. Suppressing the RECORD
would mean losing something overnight, which is a different and worse failure
than saying it late.

A malformed or disabled setting reads as "never quiet": under-suppressing a
setting the user has not actually configured is safer than silently withholding
everything because of a typo they cannot see.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

_TIME = re.compile(r"^(\d{1,2}):(\d{2})$")


def _minutes(value: Any) -> int | None:
    match = _TIME.match(str(value or "").strip())
    if not match:
        return None
    hours, minutes = int(match.group(1)), int(match.group(2))
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        return None
    return hours * 60 + minutes


def is_quiet_now(now: datetime | None = None, quiet_hours: dict[str, Any] | None = None) -> bool:
    if quiet_hours is None:
        from ..prefs import get_prefs
        quiet_hours = get_prefs().get("quietHours") or {}
    if not quiet_hours.get("enabled"):
        return False
    start = _minutes(quiet_hours.get("start"))
    end = _minutes(quiet_hours.get("end"))
    if start is None or end is None or start == end:
        return False
    now = now or datetime.now()
    current = now.hour * 60 + now.minute
    if start < end:
        return start <= current < end
    return current >= start or current < end     # wraps midnight, e.g. 23:00 -> 08:00
