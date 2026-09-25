"""What time and date it is, from this machine's own clock."""

from __future__ import annotations

from datetime import datetime

from ..capabilities import CapabilitySpec, Risk


def _run(include_date: bool = False) -> dict:
    now = datetime.now()
    # Built by hand rather than with %-I / %-d: those strip-leading-zero
    # directives are glibc-only and raise on Windows, which is this app's own
    # target platform.
    hour = now.hour % 12 or 12
    clock = f"{hour}:{now.minute:02d} {'AM' if now.hour < 12 else 'PM'}"
    date = f"{now:%A, %B} {now.day}, {now.year}"
    return {
        "iso": now.astimezone().isoformat(timespec="seconds"),
        "speak": f"It's {clock} on {date}." if include_date else f"It's {clock}.",
        "friendly_time": clock,
        "friendly_date": date,
    }


SPEC = CapabilitySpec(
    id="builtin.get_time",
    name="get_time",
    description=("Get the current date and/or time on the user's computer. Use this for any "
                 "question about what time it is, what day or date it is."),
    input_schema={"type": "object", "properties": {
        "include_date": {"type": "boolean", "description": "Also say the date."}},
        "required": []},
    risk=Risk.LOW,
    handler=_run,
    timeout_s=5.0,
    # `speak` in the result is what lets the fast path answer this with no model
    # call at all — see orchestrator/pipeline.py's `_spoken_result`.
    tags=frozenset({"core"}),
)
