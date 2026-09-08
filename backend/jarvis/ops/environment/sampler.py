"""Taking a real reading of CPU, memory and this process's own footprint.

psutil rather than a hand-rolled read: the platform-specific one this replaces
had to diff two `os.cpus()` snapshots because the obvious call returns zeros on
Windows, which is the platform this actually ships on. One dependency removes an
entire class of "correct on the machine it was written on" bug.

**The first CPU reading of a process is genuinely unknown, not zero.** A
percentage is a measurement over an interval, and there is no interval yet. It
is reported as None, and the baseline ignores it, rather than being recorded as
an idle machine that never was.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

import psutil

from . import store

logger = logging.getLogger(__name__)

SAMPLE_INTERVAL_S = 30.0
PRUNE_EVERY_N = 200

ENABLE_ENV = "JARVIS_ENV_SAMPLER"

_timer: threading.Timer | None = None
_samples_taken = 0
_primed = False


def _cpu_percent() -> float | None:
    """Busy percentage since the previous call. None on the very first one."""
    global _primed
    value = psutil.cpu_percent(interval=None)
    if not _primed:
        _primed = True
        return None                      # no interval to have measured over yet
    return float(value)


def read_now() -> dict[str, Any]:
    """A reading, without recording it — the pull path's "how loaded is it right
    now". Recording here would double-count against the regular cadence."""
    memory = psutil.virtual_memory()
    return {
        "cpuPct": _cpu_percent(),
        "memFreePct": float(memory.available) / float(memory.total) * 100 if memory.total else None,
        "rssBytes": int(psutil.Process().memory_info().rss),
        "freeBytes": int(memory.available),
        "totalBytes": int(memory.total),
        "cpuCount": psutil.cpu_count(logical=True),
    }


def take_sample() -> dict[str, Any]:
    global _samples_taken
    reading = read_now()
    store.record_sample(cpu_pct=reading["cpuPct"], mem_free_pct=reading["memFreePct"],
                        rss_bytes=reading["rssBytes"])
    _samples_taken += 1
    if _samples_taken % PRUNE_EVERY_N == 0:
        try:
            store.prune_old()
        except Exception:  # noqa: BLE001 — housekeeping must not break sampling
            logger.exception("pruning env samples failed")
    return reading


def is_enabled() -> bool:
    return os.environ.get(ENABLE_ENV) == "1"


def start() -> bool:
    global _timer
    if not is_enabled() or _timer is not None:
        return False

    def run() -> None:
        global _timer
        try:
            take_sample()
        except Exception:  # noqa: BLE001
            logger.exception("taking a system sample failed")
        _timer = threading.Timer(SAMPLE_INTERVAL_S, run)
        _timer.daemon = True
        _timer.start()

    run()
    return True


def stop() -> None:
    global _timer
    if _timer is not None:
        _timer.cancel()
        _timer = None


def reset_for_tests() -> None:
    global _samples_taken, _primed
    stop()
    _samples_taken = 0
    _primed = False
