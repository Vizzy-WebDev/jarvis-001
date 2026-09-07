"""'Unusually high, or climbing' — defined against this machine, and sustained.

Two decisions do all the work here, and both exist because the alternative
produces a finding nobody can act on:

* **Compared against this machine's own recent median**, never a fixed
  percentage. "80% CPU" means something different on a laptop that idles at 40%
  and one that idles at 3%.
* **Sustained for a real duration**, never a single sample. A build, a video
  call or opening a large file spikes the machine constantly; a notification for
  each is noise, and noise is how a real one gets ignored.

Pure: it is handed samples, or reads them, and returns a finding or None. No
model call, no interruption decision — that judgment belongs to the heartbeat.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from ...jscompat import to_iso_z
from . import store

BASELINE_WINDOW = timedelta(hours=1)
#: How far above this machine's own median counts as elevated at all.
ELEVATED_MULTIPLIER = 1.5
#: Below this share of memory free is a concern whatever the history says.
LOW_FREE_MEMORY_PCT = 10.0
SUSTAINED = timedelta(minutes=5)
#: A near-idle machine has a tiny median, so 1.5x of it is still nothing. Without
#: this, a median of 1% would report 1.6% as "unusually high".
MINIMUM_INTERESTING_CPU_PCT = 5.0
#: "Unusually high" needs something to be unusual against.
MINIMUM_SAMPLES = 5


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _at(sample: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(str(sample["ts"]).replace("Z", "+00:00"))


def _covers_the_full_duration(window: list[dict[str, Any]], cutoff: datetime) -> bool:
    """The window must actually REACH back to the cutoff. A run of three elevated
    samples in the last ninety seconds is not five sustained minutes — and
    without this check it reads as though it were."""
    if not window:
        return False
    oldest = _at(window[-1])
    return oldest <= cutoff + timedelta(minutes=1)


def check_for_anomaly(samples: list[dict[str, Any]] | None = None,
                      now: datetime | None = None) -> dict[str, Any] | None:
    now = now or datetime.now(timezone.utc)
    if samples is None:
        samples = store.list_recent(since_iso=to_iso_z(now - BASELINE_WINDOW), limit=1000)

    with_cpu = [s for s in samples if isinstance(s.get("cpuPct"), (int, float))]
    if len(with_cpu) < MINIMUM_SAMPLES:
        return None                      # no real baseline yet; silence is correct

    newest_first = sorted(with_cpu, key=_at, reverse=True)
    cutoff = now - SUSTAINED
    window = [s for s in newest_first if _at(s) >= cutoff]

    median = _median([float(s["cpuPct"]) for s in with_cpu])
    threshold = median * ELEVATED_MULTIPLIER

    if (threshold > MINIMUM_INTERESTING_CPU_PCT and window
            and all(float(s["cpuPct"]) >= threshold for s in window)
            and _covers_the_full_duration(window, cutoff)):
        minutes = int(SUSTAINED.total_seconds() // 60)
        return {
            "summary": (f"CPU load has stayed high for the last {minutes} minutes — "
                        f"around {round(float(window[0]['cpuPct']))}%, against a recent "
                        f"typical {round(median)}% on this machine."),
            "detail": json.dumps({"kind": "cpu", "current": window[0]["cpuPct"],
                                  "median": median,
                                  "sustainedMinutes": minutes}),
        }

    with_memory = [s for s in newest_first
                   if isinstance(s.get("memFreePct"), (int, float)) and _at(s) >= cutoff]
    if (with_memory and all(float(s["memFreePct"]) < LOW_FREE_MEMORY_PCT for s in with_memory)
            and _covers_the_full_duration(with_memory, cutoff)):
        minutes = int(SUSTAINED.total_seconds() // 60)
        return {
            "summary": (f"Free memory has been under {round(LOW_FREE_MEMORY_PCT)}% for the "
                        f"last {minutes} minutes — around "
                        f"{round(float(with_memory[0]['memFreePct']))}% free now."),
            "detail": json.dumps({"kind": "memory", "current": with_memory[0]["memFreePct"],
                                  "sustainedMinutes": minutes}),
        }
    return None
