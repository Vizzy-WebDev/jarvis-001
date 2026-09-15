"""Scheduling something for later, and seeing or cancelling what is scheduled.

Creating a task is MEDIUM risk: it commits the assistant to acting at a time
nobody will be watching, which is a different thing from answering now. So it is
read back before it is saved — and the read-back says what the schedule actually
means in plain English ("weekdays at 9:30 AM"), because "recurrence type
weekdays" is not something anyone can confirm out loud.

Listing is LOW. Cancelling is MEDIUM: it stops something the user set up, and
doing that silently on a misheard name is exactly the kind of quiet loss that is
hard to notice and impossible to undo from the transcript.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from ..capabilities import CapabilitySpec, Risk
from ..scheduler import briefing_config, task_store
from ..scheduler.recurrence import describe, next_run_at

_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _recurrence_from(when: str, time: str | None, days: list[int] | None,
                     every_minutes: int | None) -> dict[str, Any] | str:
    """Turn the model's arguments into a recurrence, or say what is wrong.

    Returned as a string on failure rather than raised: a malformed schedule is
    something to tell the user about, not an exception to surface as a crash.
    """
    kind = (when or "").strip().lower()
    if kind in ("daily", "every day"):
        kind = "daily"
    if kind in ("weekdays", "weekday"):
        kind = "weekdays"

    if kind in ("daily", "weekdays", "weekly"):
        if not time or not _TIME.match(time):
            return "I need a time of day, as HH:MM on a 24-hour clock."
        spec: dict[str, Any] = {"type": kind, "time": time}
        if kind == "weekly":
            chosen = [d for d in (days or []) if isinstance(d, int) and 0 <= d <= 6]
            if not chosen:
                return "For a weekly task I need which days, as 0 for Sunday to 6 for Saturday."
            spec["days"] = chosen
        return spec

    if kind in ("once", "one-off"):
        if not time or not _TIME.match(time):
            return "I need a time of day, as HH:MM on a 24-hour clock."
        hour, minute = (int(p) for p in time.split(":"))
        at = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
        if at <= datetime.now():
            at += timedelta(days=1)     # "at 7" said at 9pm means tomorrow
        from ..jscompat import to_iso_z

        return {"type": "once", "at": to_iso_z(at)}

    if kind in ("interval", "every"):
        if not every_minutes or every_minutes < 1:
            return "For a repeating interval I need how many minutes between runs."
        return {"type": "interval", "everyMs": int(every_minutes) * 60000}

    return ('I did not understand that schedule. Use "daily", "weekdays", "weekly", '
            '"once", or "interval".')


def _action_from(kind: str, text: str) -> dict[str, Any]:
    if (kind or "").strip().lower() == "briefing":
        return {"type": "briefing"}
    if (kind or "").strip().lower() == "reminder":
        # A reminder is its own text: nothing to work out, nothing to get wrong.
        return {"type": "message", "text": text}
    return {"type": "prompt", "text": text}


def _schedule(title: str = "", when: str = "", time: str | None = None,
              action: str = "reminder", text: str = "", days: list[int] | None = None,
              every_minutes: int | None = None) -> dict[str, Any]:
    recurrence = _recurrence_from(when, time, days, every_minutes)
    if isinstance(recurrence, str):
        return {"ok": False, "error": recurrence}
    task = task_store.create_task(
        title=title or text or describe(recurrence),
        recurrence=recurrence,
        action=_action_from(action, text or title))
    return {"ok": True, "id": task["id"], "title": task["title"],
            "schedule": describe(recurrence), "nextRunAt": task["nextRunAt"],
            "speak": f"Set: {task['title']}, {describe(recurrence)}."}


def _schedule_summary(args: dict[str, Any]) -> str:
    recurrence = _recurrence_from(str(args.get("when") or ""), args.get("time"),
                                  args.get("days"), args.get("every_minutes"))
    if isinstance(recurrence, str):
        return recurrence
    what = args.get("title") or args.get("text") or "this"
    upcoming = next_run_at(recurrence, datetime.now())
    first = ""
    if upcoming:
        hour = upcoming.hour % 12 or 12
        first = (f" First one {upcoming:%A}, {hour}:{upcoming.minute:02d} "
                 f"{'AM' if upcoming.hour < 12 else 'PM'}.")
    return f'Schedule "{what}" {describe(recurrence)}?{first}'


def _list(include_disabled: bool = False) -> dict[str, Any]:
    tasks = task_store.list_tasks()
    if not include_disabled:
        tasks = [t for t in tasks if t.get("enabled")]
    if not tasks:
        return {"ok": True, "tasks": [], "speak": "Nothing is scheduled."}
    return {"ok": True, "tasks": [
        {"id": t["id"], "title": t["title"], "schedule": describe(t.get("recurrence")),
         "nextRunAt": t.get("nextRunAt"), "enabled": bool(t.get("enabled")),
         "lastRunAt": t.get("lastRunAt")}
        for t in tasks]}


def _find_task(query: str) -> dict[str, Any] | None:
    wanted = (query or "").strip().lower()
    if not wanted:
        return None
    tasks = task_store.list_tasks()
    for task in tasks:
        if task["id"] == query or task["title"].lower() == wanted:
            return task
    return next((t for t in tasks if wanted in t["title"].lower()), None)


def _cancel(query: str = "") -> dict[str, Any]:
    task = _find_task(query)
    if task is None:
        return {"ok": False, "error": f'I couldn\'t find a scheduled task matching "{query}".'}
    task_store.delete_task(task["id"])
    return {"ok": True, "cancelled": task["title"], "speak": f"Cancelled {task['title']}."}


def _cancel_summary(args: dict[str, Any]) -> str:
    task = _find_task(str(args.get("query") or ""))
    if task is None:
        return f'I couldn\'t find a scheduled task matching "{args.get("query")}".'
    return f'Cancel "{task["title"]}" ({describe(task.get("recurrence"))})?'


def _configure_briefing(weather_place: str | None = None, headlines: bool | None = None,
                        include: list[str] | None = None,
                        exclude: list[str] | None = None,
                        custom_text: str | None = None) -> dict[str, Any]:
    patch: dict[str, Any] = {}
    sections: dict[str, bool] = {}
    for name in include or []:
        sections[name] = True
    for name in exclude or []:
        sections[name] = False
    if sections:
        patch["sections"] = sections
    if weather_place is not None:
        patch["weatherPlace"] = weather_place.strip()
    if headlines is not None:
        patch["headlines"] = bool(headlines)
    if custom_text is not None:
        patch["customText"] = custom_text
        patch.setdefault("sections", {})["custom"] = bool(custom_text.strip())

    config = briefing_config.set_config(patch)
    return {"ok": True, "config": config,
            "speak": "Updated what goes into your briefing."}


SPECS = [
    CapabilitySpec(
        id="builtin.schedule_task", name="schedule_task",
        description=("Schedule something for later — a reminder, a recurring question for "
                     "Jarvis to answer, or the morning briefing. Use for anything the user "
                     "wants to happen at a time rather than now."),
        input_schema={"type": "object", "properties": {
            "title": {"type": "string", "description": "Short name for the task."},
            "when": {"type": "string",
                     "description": 'One of "daily", "weekdays", "weekly", "once", "interval".'},
            "time": {"type": "string", "description": "Time of day as HH:MM, 24-hour."},
            "days": {"type": "array", "description": "For weekly: 0 = Sunday to 6 = Saturday."},
            "every_minutes": {"type": "integer", "description": "For interval: minutes apart."},
            "action": {"type": "string",
                       "description": '"reminder" (say this text), "prompt" (work this out '
                                      'at the time), or "briefing".'},
            "text": {"type": "string", "description": "The reminder text, or what to work out."}},
            "required": ["when"]},
        # It commits the assistant to acting when nobody is watching.
        risk=Risk.MEDIUM, handler=_schedule, summarize=_schedule_summary,
        timeout_s=10.0, tags=frozenset({"meta"}),
    ),
    CapabilitySpec(
        id="builtin.list_tasks", name="list_tasks",
        description="List what is currently scheduled, and when each next runs.",
        input_schema={"type": "object", "properties": {
            "include_disabled": {"type": "boolean"}}, "required": []},
        risk=Risk.LOW, handler=_list, timeout_s=10.0, tags=frozenset({"meta"}),
    ),
    CapabilitySpec(
        id="builtin.cancel_task", name="cancel_task",
        description="Cancel a scheduled task the user asks to stop.",
        input_schema={"type": "object", "properties": {
            "query": {"type": "string", "description": "Which task, in their own words."}},
            "required": ["query"]},
        risk=Risk.MEDIUM, handler=_cancel, summarize=_cancel_summary,
        timeout_s=10.0, tags=frozenset({"meta"}),
    ),
    CapabilitySpec(
        id="builtin.configure_briefing", name="configure_briefing",
        description=("Change what goes into the morning briefing — the place for weather, "
                     "whether to include headlines, which sections to include, or an extra "
                     "instruction of their own."),
        input_schema={"type": "object", "properties": {
            "weather_place": {"type": "string",
                              "description": "City for weather. Empty string turns it off."},
            "headlines": {"type": "boolean"},
            "include": {"type": "array", "description": "Section names to turn on."},
            "exclude": {"type": "array", "description": "Section names to turn off."},
            "custom_text": {"type": "string",
                            "description": "Something extra they always want included."}},
            "required": []},
        risk=Risk.MEDIUM, handler=_configure_briefing, timeout_s=10.0,
        tags=frozenset({"meta"}),
    ),
]
