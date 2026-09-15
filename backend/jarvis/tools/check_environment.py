"""Can this actually be done right now, on this machine, at this moment?

A different question from "is Jarvis broken" (`check_my_health`) and from "am I
any good at this" (`check_myself`) — those are about Jarvis; this is about the
conditions it is working in. Answering "sure, I'll transcode that" while the
machine has been pinned at 98% for ten minutes is the failure this exists to
prevent.
"""

from __future__ import annotations

from typing import Any

from ..capabilities import CapabilitySpec, Risk


def _run() -> dict[str, Any]:
    from ..ops.environment import baseline, reachability, sampler

    reading = sampler.read_now()
    anomaly = baseline.check_for_anomaly()
    reach = reachability.snapshot()

    load: dict[str, Any] = {
        "cpuPct": None if reading["cpuPct"] is None else round(reading["cpuPct"], 1),
        "memoryFreePct": None if reading["memFreePct"] is None else round(reading["memFreePct"], 1),
        "cpuCount": reading["cpuCount"],
    }
    if reading["cpuPct"] is None:
        # A percentage is a measurement over an interval, and the first reading
        # of a process has no interval yet. Unknown, not idle.
        load["note"] = "CPU use is not known yet — this is the first reading since starting."

    return {
        "ok": True,
        "load": load,
        "unusual": anomaly["summary"] if anomaly else None,
        "models": reach["models"],
        "voice": reach["voice"],
        "note": ("Real readings from this machine, plus what is actually reachable. "
                 "If nothing is unusual, say so plainly rather than hedging."),
    }


SPEC = CapabilitySpec(
    id="builtin.check_environment",
    name="check_environment",
    description=("Check what this computer and Jarvis's own connections can handle right now — "
                 "how loaded the machine is, whether load has been unusually high, and which "
                 "models and voices are actually reachable. Use it before taking on something "
                 "heavy, or when asked whether you can do something right now."),
    input_schema={"type": "object", "properties": {}, "required": []},
    risk=Risk.LOW,
    handler=_run,
    timeout_s=15.0,
    tags=frozenset({"core", "meta"}),
)
