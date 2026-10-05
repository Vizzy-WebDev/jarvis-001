"""A specialist run that a restart cut off, started again as durable work.

A run Jarvis asked for lives on a thread (`runner.run_agent`); when Jarvis stopped, the run
stopped with it and its result could never arrive — it sat "running" until the next start
closed it off. Now, at startup, a run Jarvis itself asked for (a root run, requested by Jarvis
or the person) that a restart cut off is started again from its task, as one round of durable
work (`jarvis/durable.py`), with nobody waiting: its result reaches the person exactly as a
late result does today (`runner._deliver_late` — the chat, a notification, and a notice Jarvis
passes on). Nothing of the cut-off attempt was saved, so it starts over; from here on it is
durable, and a second restart picks it up where it was, once. Nested runs (one specialist
asking another) and runs inside a job are closed off as before — the asker, or the job's own
recovery, decides what happens next.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Iterator

from .. import durable
from ..policy import Autonomy, Surface
from . import store

logger = logging.getLogger(__name__)

KIND = "agent_run"

#: Restarts a started-again run survives: picked back up once, then reported as unfinished.
MAX_RECOVERIES = 1

#: Who may ask for a run that is worth starting again: Jarvis or the person, not a specialist.
ROOT_ASKERS = ("jarvis", "operator")


def _session_for(run_id: str) -> str:
    return (store.get_run(run_id) or {}).get("sessionId") or f"agent-run:{run_id}"


def _turn(run_id: str, text: str, spec: Any, cancel: threading.Event) -> Iterator[Any]:
    from ..orchestrator import Failed
    from . import runner

    run = store.get_run(run_id)
    if run is None:
        yield Failed("The specialist run no longer exists.")
        return
    try:
        agent = runner.usable_agent(run["agentId"])
    except runner.AgentUnavailable as err:
        yield Failed(str(err))
        return
    # Nobody's turn is waiting for it: its finish is announced as a late result.
    with runner._detached_lock:
        runner._detached.add(run_id)
    yield from runner.stream_run(agent, run, text, autonomy=Autonomy.ESCALATE,
                                 surface=Surface.SCHEDULED, cancel=cancel, turn=spec)


def _finish(run_id: str, outcome: dict[str, Any]) -> dict[str, Any]:
    _deliver_once(run_id, outcome.get("files") or [])
    return {"runId": run_id, "status": (store.get_run(run_id) or {}).get("status")}


def _deliver_once(run_id: str, files: list[dict[str, Any]]) -> None:
    """A finish replayed after a crash must not tell the person twice."""
    from ..heartbeat import outbox
    from . import runner

    if outbox.for_source_ref("agent", run_id):
        return
    runner._deliver_late(run_id, files)


durable.register(durable.Kind(name=KIND, session_for=_session_for, turn=_turn, finish=_finish))


def recover_at_startup() -> dict[str, list[str]]:
    """Start again the root runs this start closed off, and pick back up started-again runs
    a restart interrupted. Each on its own thread; returns which, for the record."""
    from ..assembly import get_registry
    from ..background import run_in_background
    from .capabilities import closed_at_startup

    # Building the registry is what closes off orphaned runs; it has to have happened
    # before anything here starts a run, or a fresh run could be closed as an orphan.
    get_registry()
    out: dict[str, list[str]] = {"started_again": [], "resumed": []}

    if _durable_file_exists():
        for work in durable.with_status(KIND, "running"):
            run_in_background(lambda run_id=work["id"]: _recover(run_id),
                              name=f"agent-run-recovery-{work['id']}")
            out["resumed"].append(work["id"])

    while closed_at_startup:
        orphan = closed_at_startup.pop(0)
        if orphan.get("parentRunId") or orphan.get("jobId") \
                or orphan.get("requestedBy") not in ROOT_ASKERS:
            continue
        replacement = _start_again(orphan)
        if replacement is not None:
            out["started_again"].append(replacement)
    return out


def _durable_file_exists() -> bool:
    from ..store import data_dir

    return (data_dir() / durable.FILE_NAME).exists()


def _start_again(orphan: dict[str, Any]) -> str | None:
    from ..background import run_in_background
    from . import runner

    try:
        agent = runner.usable_agent(orphan["agentId"])
    except runner.AgentUnavailable:
        return None
    # Its own working session: the specialist's conversation session may be in use again
    # by the time this runs, and a durable round restores its session wholesale.
    run = runner.start_run(agent, orphan["task"], session_id=f"{orphan['id']}:again",
                           requested_by=orphan["requestedBy"],
                           conversation_id=orphan.get("conversationId"))
    store.finish_run(orphan["id"], status="failed", result=orphan.get("result"),
                     error=f"Jarvis restarted before this finished, so it was started again "
                           f"({run['id']}).", tools_used=orphan.get("toolsUsed"))
    message = runner._message_for(orphan["task"], None, orphan["requestedBy"])
    run_in_background(lambda: durable.advance(run["id"], KIND, message),
                      name=f"agent-run-again-{run['id']}")
    return run["id"]


def _recover(run_id: str) -> None:
    reached = durable.recover(run_id, max_recoveries=MAX_RECOVERIES)
    if not reached or reached.get("state") not in ("abandoned", "unsure"):
        return
    run = store.get_run(run_id) or {}
    if reached["state"] == "unsure":
        names = ", ".join(sorted({a["capability"] for a in reached.get("actions") or []}))
        error = (f"Jarvis stopped in the middle of {names}, which reaches outside Jarvis, so "
                 "it may or may not have happened. It was not repeated — please check.")
    else:
        error = "Jarvis restarted twice while this was running, so it was not tried again."
    if run and run.get("status") != "done":
        store.finish_run(run_id, status="failed", result=run.get("result"), error=error,
                         tools_used=run.get("toolsUsed"))
    _deliver_once(run_id, [])
    durable.abandon(run_id, {"runId": run_id, "status": "failed"})
