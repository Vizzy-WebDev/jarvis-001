"""What actually calls `reflect()`/`synthesize()`. Neither was ever wired to a
clock — the outcomes were piling up, `capture.py` was writing them (once the
observer wiring in `jarvis/observers/improvement.py` connected it), and
nothing ever turned them into lessons.

Deliberately thin: `reflect()`/`synthesize()` already gate themselves on
enough-new-material, their own `CADENCE_HOURS`, and `store.py`'s daily budget
— see each function's own early-return checks. So this tick needs no cadence
or budget logic of its own; it just has to run often enough that the real
gates get a chance to say yes. Same `threading.Timer`, re-armed-after-each-tick
shape as `heartbeat/engine.py`, `scheduler/engine.py`, `monitor/engine.py` and
`jobs/orchestrator.py`'s own timers.
"""

from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger(__name__)

#: Reflect's own cadence is 4h, synthesize's is 24h (see reflect.py/
#: synthesize.py's own CADENCE_HOURS) — this only needs to poll often enough
#: to catch that moment, not to gate anything itself.
TICK_SECONDS = 900.0
ENABLE_ENV = "JARVIS_IMPROVEMENT"

_timer: threading.Timer | None = None


def is_enabled() -> bool:
    return os.environ.get(ENABLE_ENV) == "1"


def tick() -> dict[str, dict]:
    """One pass: try reflect, then synthesize. Returns what each did (or why
    it didn't) — for tests, and for a real "what happened" answer."""
    from .reflect import reflect
    from .synthesize import synthesize

    return {"reflect": reflect(), "synthesize": synthesize()}


def start() -> bool:
    """Start the tick, if the interlock allows it. Returns whether it started —
    same shape as every other subsystem's own start()."""
    global _timer
    if not is_enabled() or _timer is not None:
        return False

    def run() -> None:
        global _timer
        try:
            tick()
        except Exception:  # noqa: BLE001
            logger.exception("an improvement cadence tick failed")
        _timer = threading.Timer(TICK_SECONDS, run)
        _timer.daemon = True
        _timer.start()

    run()
    return True


def stop() -> None:
    global _timer
    if _timer is not None:
        _timer.cancel()
        _timer = None
