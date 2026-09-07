"""Is the thing that records what capabilities did still working?

A broken recorder and a capability genuinely never used look identical from
every dimension that reads the stats — which is why the recorder logs its own
successes and failures, and why this reads that log rather than the stats.
"""

from __future__ import annotations

from typing import Any

from ..registry import Check

#: Any failure at all is worth reporting: this records a handful of rows a turn,
#: so a failure is a real fault rather than an unlucky one.
FAILURE_THRESHOLD = 1


def probe() -> dict[str, Any]:
    from ....self import store as self_store

    summary = self_store.capture_health_summary(hours=24)
    failures = int(summary.get("failures") or 0)
    if failures >= FAILURE_THRESHOLD:
        return {"ok": False,
                "detail": (f"The capability recorder failed {failures} time(s) in the last "
                           f"24 hours, so what it says about reliability is incomplete.")}
    return {"ok": True}


CHECK = Check(id="capture_health", probe=probe)
