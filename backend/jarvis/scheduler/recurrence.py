"""Scheduling maths — pure, dependency-free, and hand-rolled on purpose.

The shape set is small, and computing it here means `describe()` can produce the
exact plain-English sentence the UI and the spoken read-back need, for free. A
cron library would give neither, and cron syntax is not something to read back to
someone out loud.

A recurrence is one of:

    {"type": "once",     "at": ISO}
    {"type": "daily",    "time": "HH:MM"}
    {"type": "weekdays", "time": "HH:MM"}                 # Mon-Fri
    {"type": "weekly",   "time": "HH:MM", "days": [0-6]}  # 0 = Sunday
    {"type": "interval", "everyMs": int, "anchor": ISO}

All time-of-day maths uses the local clock: this runs on the user's own machine,
so their local time is the only one that means anything. The day-rollover path
re-pins the clock time AFTER adding a day rather than adding 24 hours, so a
schedule stays at "8am" across a daylight-saving change instead of drifting to
7am or 9am.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

DAY_NAMES = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
WEEKDAYS = (1, 2, 3, 4, 5)


def _parse_time(value: str | None) -> tuple[int, int]:
    parts = str(value or "00:00").split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        return 0, 0
    return max(0, min(23, hour)), max(0, min(59, minute))


def _at_time(moment: datetime, value: str | None) -> datetime:
    hour, minute = _parse_time(value)
    return moment.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _weekday_number(moment: datetime) -> int:
    """0 = Sunday, matching the stored spec (and JavaScript's getDay)."""
    return (moment.weekday() + 1) % 7


def _next_daily_like(start: datetime, value: str | None,
                     allowed_days: tuple[int, ...] | list[int] | None) -> datetime | None:
    candidate = _at_time(start, value)
    for _ in range(8):
        if candidate >= start and (allowed_days is None
                                   or _weekday_number(candidate) in allowed_days):
            return candidate
        candidate = _at_time(candidate + timedelta(days=1), value)
    return None


def next_run_at(spec: dict[str, Any] | None, start: datetime | None = None) -> datetime | None:
    """The next occurrence at or after `start`, or None if there are no more."""
    start = start or datetime.now()
    if not spec or not spec.get("type"):
        return None
    kind = spec["type"]

    if kind == "once":
        at = _parse_iso(spec.get("at"))
        return at if at and at >= start else None
    if kind == "daily":
        return _next_daily_like(start, spec.get("time"), None)
    if kind == "weekdays":
        return _next_daily_like(start, spec.get("time"), WEEKDAYS)
    if kind == "weekly":
        days = spec.get("days") or [_weekday_number(start)]
        return _next_daily_like(start, spec.get("time"), list(days))
    if kind == "interval":
        every_ms = spec.get("everyMs")
        if not isinstance(every_ms, (int, float)) or every_ms <= 0:
            return None
        anchor = _parse_iso(spec.get("anchor")) or start
        elapsed_ms = (start - anchor).total_seconds() * 1000
        steps = max(0, -(-elapsed_ms // every_ms))       # ceil, without float drift
        return anchor + timedelta(milliseconds=steps * every_ms)
    return None


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone().replace(tzinfo=None) if parsed.tzinfo else parsed


def _clock(value: str | None) -> str:
    hour, minute = _parse_time(value)
    display = hour % 12 or 12
    return f"{display}:{minute:02d} {'AM' if hour < 12 else 'PM'}"


def describe(spec: dict[str, Any] | None) -> str:
    """Plain English, for the screen and for saying out loud."""
    if not spec or not spec.get("type"):
        return "never"
    kind = spec["type"]

    if kind == "once":
        at = _parse_iso(spec.get("at"))
        if at is None:
            return "once"
        hour = at.hour % 12 or 12
        return (f"once, on {DAY_NAMES[_weekday_number(at)]}, "
                f"{at:%B} {at.day} at {hour}:{at.minute:02d} "
                f"{'AM' if at.hour < 12 else 'PM'}")
    if kind == "daily":
        return f"every day at {_clock(spec.get('time'))}"
    if kind == "weekdays":
        return f"weekdays at {_clock(spec.get('time'))}"
    if kind == "weekly":
        names = [DAY_NAMES[d] for d in (spec.get("days") or []) if 0 <= d < 7]
        return f"every {', '.join(names) or 'week'} at {_clock(spec.get('time'))}"
    if kind == "interval":
        minutes = round((spec.get("everyMs") or 0) / 60000)
        if minutes and minutes % 1440 == 0:
            days = minutes // 1440
            return f"every {days} day{'' if days == 1 else 's'}"
        if minutes and minutes % 60 == 0:
            hours = minutes // 60
            return f"every {hours} hour{'' if hours == 1 else 's'}"
        return f"every {minutes} minute{'' if minutes == 1 else 's'}"
    return "never"
