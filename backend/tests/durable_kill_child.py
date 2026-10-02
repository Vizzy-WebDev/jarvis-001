"""The child process for `test_durable_crash.py`'s real-kill test. Not a test itself.

`first`: create a three-round job and run it; at round 2 the model call never returns, so
the parent can SIGKILL this process mid-round. `second`: a fresh process doing exactly what a
real start does for jobs — `recover_orphans()` — and waiting for it to finish.

Every model call is appended (and flushed to disk) to the log named on the command line, as
`[process, round, step]`, before it is answered — so the parent sees exactly what was asked.
"""

from __future__ import annotations

import json
import os
import sys
import threading

assert os.environ.get("JARVIS_DATA_DIR"), "refusing to run without a scratch data dir"

MODE, LOG = sys.argv[1], sys.argv[2]

from durable_support import Brain, install, position, register_effects  # noqa: E402
from jarvis.events.bus import EventBus  # noqa: E402
from jarvis.jobs import job_store, orchestrator, worker  # noqa: E402
from jarvis.orchestrator.pipeline import MAX_STEPS  # noqa: E402

worker._verify_result = lambda job, answer: None  # no real model to check the result with


class LoggedBrain(Brain):
    def stream(self, **kw):  # type: ignore[override]
        round_no, step = position(kw["messages"])
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps([MODE, round_no, step]) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        if MODE == "first" and (round_no, step) == (2, 0):
            threading.Event().wait()  # never returns: killed here
        return super().stream(**kw)


def lookups(n: int, start: int) -> list[tuple]:
    return [("call", "look_up_fact", {"q": f"fact {i}"}) for i in range(start, start + n)]


register_effects()
brain = install(LoggedBrain())
brain.plan("job:", [
    [("call", "effect_low", {"tag": "a"})] + lookups(MAX_STEPS - 1, 0),
    [("call", "effect_low", {"tag": "b"})] + lookups(MAX_STEPS - 1, 10),
    [("call", "effect_low", {"tag": "c"}), ("say", "All three rounds done.")],
])

if MODE == "first":
    job = job_store.create_job(title="Three rounds", goal="do three rounds of work")
    worker.run_job(job["id"], event_bus=EventBus())
    sys.exit(3)  # unreachable: the parent kills it at round 2
else:
    print(orchestrator.recover_orphans(event_bus=EventBus()))
    assert worker.join_all(timeout=60)
    print([(j["id"], j["status"]) for j in job_store.list_jobs()])
