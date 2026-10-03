"""An unusual burst of the things that ought to be rare.

Failed capability calls and refused approvals happen; a sudden run of them does
not, and is worth a person's attention whether the cause is a bug, a confused
model or something worse. Detection only — the right response depends entirely
on which of those it turns out to be.

Compared against this install's own recent history, not a fixed number, for the
same reason the load baseline is: "twenty refusals" means something different on
a machine that normally sees none than on one mid-experiment.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .....jscompat import to_iso_z
from ...registry import Check

RECENT = timedelta(hours=1)
BASELINE = timedelta(days=7)
#: How many times the hourly rate must exceed the recent typical rate.
SPIKE_MULTIPLIER = 5.0
#: Below this, a "spike" is two or three events and means nothing.
MINIMUM_TO_CARE = 10


def probe() -> dict[str, Any]:
    from . import counters

    now = datetime.now(timezone.utc)
    findings = []
    for kind, label in (("tool_failed", "capability failures"),
                        ("approval_denied", "refused confirmations")):
        recent = counters.count_since(kind, to_iso_z(now - RECENT))
        if recent < MINIMUM_TO_CARE:
            continue
        week = counters.count_since(kind, to_iso_z(now - BASELINE))
        typical_per_hour = max(week / (BASELINE.total_seconds() / 3600), 0.0)
        if typical_per_hour <= 0 or recent > typical_per_hour * SPIKE_MULTIPLIER:
            findings.append(f"{recent} {label} in the last hour")
    if findings:
        return {"ok": False,
                "detail": ("Something is happening far more often than usual: "
                           + "; ".join(findings) + ". Worth a look.")}
    return {"ok": True}


CHECK = Check(id="event_spikes", probe=probe, kind="security")
