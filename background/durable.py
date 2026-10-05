"""Durable background work: what finished is saved, so a crash resumes instead of restarting.

Background work (a job, a scheduled run, a specialist run nobody is waiting on) runs in
**rounds**, and each round is one ordinary turn through the ONE turn loop
(`orchestrator/pipeline.py`) — there is no second agent loop here. What this module adds is
the record: every finished round's outcome and working transcript is saved, so after a crash
or a restart the work picks up at the round that was running, with everything it had learned,
instead of starting over from the goal.

LangGraph is used for that persistence and orchestration ONLY (its functional API: an
`@entrypoint` per piece of work, one `@task` per round; a wait for a person ends a run) —
never for the live conversation, never as a model client. Checkpoints live in their own file,
`data/durable.db`, never in `jarvis.db`, whose single shared connection has rules of its own.

**A round replayed after a crash does not repeat what already happened.** Its turn runs with
`TurnRequest.operation_scope`, so a tool call gets the same operation id it had the first time
(`pipeline.operation_id_for`) and `capabilities/execute.py` returns the recorded result rather
than doing it again. Recorded FAILURES of that round are cleared first, so a transient failure
is tried again rather than replayed. Which in-flight action may not be safe to repeat (it
started and never recorded an outcome) is the caller's call, read from its own trace, before
it recovers anything (`jobs/orchestrator.recover_orphans`).

What each kind of work does is a `Kind`: how to run its turn, what to do when it has to wait
for someone, and how to finish. Everything else — the rounds, the transcript, the step
budget, pausing and continuing, recovery — is here, once.

Concurrency: one piece of work is advanced by one thread at a time (a per-work lock), and
`stop()` interrupts the round in flight through the turn's own cancel event.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from . import conversation
from .jscompat import now_iso

logger = logging.getLogger(__name__)

FILE_NAME = "durable.db"

#: What the next round of continuing work is asked, when nobody said anything more.
CONTINUE_TEXT = ("Carry on with the work from where you left off. When it is all done, give "
                 "the finished result.")

#: What the work is told about a call the person declined, in place of "needs your go-ahead".
DECLINED = "The person said no to this, so it was not done. Carry on without it."


@dataclass(frozen=True)
class TurnSpec:
    """What the runner fixes about one round's turn. A `Kind` puts these on its `TurnRequest`
    unchanged — the replay guarantees depend on them."""

    turn_id: str
    operation_scope: str
    continuable: bool
    #: The most model steps this round may take (what is left of the budget), or None.
    max_steps: int | None = None


def _nothing(*_args: Any) -> None:
    return None


@dataclass(frozen=True)
class Kind:
    """One kind of durable work.

    - `session_for(work_id)` — the working session the rounds run in.
    - `turn(work_id, text, spec, cancel)` — run one round's turn; yields the turn loop's events.
    - `finish(work_id, outcome)` — record the end. For continuing work, returning
      `{"accepted": False, "error": ...}` refuses the result and parks instead.
    - `park(work_id, why)` — record that the work is waiting (for a decision, after a
      failure, out of budget); the work then waits for `advance()` with an answer.
    - `continuable` — True: rounds continue until the work says it is finished, within
      `budget` model steps per allowance. False: exactly one round, then `finish`.
    - `on_event(work_id, event)` — each turn event as it happens (a heartbeat, say).
    - `observe(work_id, type, payload)` — every bus event of this work's session (its trace).
    - `stopped(work_id)` — read at the start of each round: True ends it before any model call.
    """

    name: str
    session_for: Callable[[str], str]
    turn: Callable[[str, str, TurnSpec, threading.Event], Iterable[Any]]
    finish: Callable[[str, dict[str, Any]], Any]
    park: Callable[[str, dict[str, Any]], None] = _nothing
    continuable: bool = False
    on_event: Callable[[str, Any], None] | None = None
    observe: Callable[[str, str, dict[str, Any]], None] | None = None
    stopped: Callable[[str], bool] | None = None


_KINDS: dict[str, Kind] = {}


def register(kind: Kind) -> None:
    _KINDS[kind.name] = kind


def _kind(name: str) -> Kind:
    if name not in _KINDS:
        # Kinds register themselves when their module is imported; work recovered at startup
        # may be advanced before anything else imported it.
        _import_kinds()
    return _KINDS[name]


def _import_kinds() -> None:
    from .agents import durable_runs  # noqa: F401
    from .jobs import worker  # noqa: F401
    from .scheduler import durable_runs as _task_runs  # noqa: F401


# --- the store --------------------------------------------------------------------------------

class _Store:
    """The checkpoint file, opened once per data directory.

    Two connections to one file: LangGraph's saver keeps its own, and the `works` table — which
    piece of work is running, waiting or finished — has another, so neither ever commits the
    other's half-done transaction.
    """

    def __init__(self, path: Path) -> None:
        from langgraph.checkpoint.sqlite import SqliteSaver

        self.path = path
        self.saver_conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self.saver = SqliteSaver(self.saver_conn)
        self.saver.setup()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30,
                                    isolation_level=None)
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS works ("
                " id TEXT PRIMARY KEY, kind TEXT NOT NULL, text TEXT NOT NULL,"
                " budget INTEGER, epoch INTEGER NOT NULL DEFAULT 0,"
                " status TEXT NOT NULL, waiting_on TEXT, result TEXT, meta TEXT,"
                " recoveries INTEGER NOT NULL DEFAULT 0,"
                " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS started ("
                " operation_id TEXT PRIMARY KEY, work_id TEXT NOT NULL, epoch INTEGER NOT NULL,"
                " capability TEXT NOT NULL, external INTEGER NOT NULL, started_at TEXT NOT NULL)")
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS rounds ("
                " work_id TEXT NOT NULL, epoch INTEGER NOT NULL, round_no INTEGER NOT NULL,"
                " outcome TEXT NOT NULL, steps INTEGER NOT NULL, finished_at TEXT NOT NULL,"
                " PRIMARY KEY (work_id, epoch, round_no))")
        self.workflow = _build_workflow(self.saver)

    def close(self) -> None:
        for conn in (self.conn, self.saver_conn):
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                logger.warning("could not close %s cleanly", self.path)

    # works rows

    def get(self, work_id: str) -> dict[str, Any] | None:
        with self.lock:
            cur = self.conn.execute("SELECT * FROM works WHERE id = ?", (work_id,))
            row = cur.fetchone()
            names = [d[0] for d in cur.description] if cur.description else []
        if row is None:
            return None
        out = dict(zip(names, row))
        for key in ("waiting_on", "result", "meta"):
            out[key] = json.loads(out[key]) if out[key] else None
        out["meta"] = out["meta"] or {}
        return out

    def insert(self, work_id: str, kind: str, text: str, budget: int | None,
               meta: dict[str, Any] | None) -> None:
        now = now_iso()
        with self.lock:
            self.conn.execute(
                "INSERT INTO works (id, kind, text, budget, epoch, status, meta, created_at,"
                " updated_at) VALUES (?, ?, ?, ?, 0, 'running', ?, ?, ?)",
                (work_id, kind, text, budget, json.dumps(meta or {}, default=str), now, now))

    def set(self, work_id: str, **fields: Any) -> None:
        for key in ("waiting_on", "result"):
            if key in fields:
                fields[key] = json.dumps(fields[key], default=str) if fields[key] is not None \
                    else None
        fields["updated_at"] = now_iso()
        assignments = ", ".join(f"{name} = ?" for name in fields)
        with self.lock:
            self.conn.execute(f"UPDATE works SET {assignments} WHERE id = ?",
                              [*fields.values(), work_id])

    def record_round(self, work_id: str, epoch: int, round_no: int, outcome: str,
                     steps: int) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO rounds (work_id, epoch, round_no, outcome, steps,"
                " finished_at) VALUES (?, ?, ?, ?, ?, ?)",
                (work_id, epoch, round_no, outcome, steps, now_iso()))

    def mark_started(self, work_id: str, epoch: int, operation_id: str, capability: str,
                     external: bool) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO started (operation_id, work_id, epoch, capability,"
                " external, started_at) VALUES (?, ?, ?, ?, ?, ?)",
                (operation_id, work_id, epoch, capability, 1 if external else 0, now_iso()))

    def started_external(self, work_id: str, epoch: int) -> list[tuple[str, str]]:
        with self.lock:
            return [(r[0], r[1]) for r in self.conn.execute(
                "SELECT operation_id, capability FROM started WHERE work_id = ? AND epoch = ?"
                " AND external = 1 ORDER BY started_at", (work_id, epoch)).fetchall()]

    def by_status(self, kind: str, status: str) -> list[dict[str, Any]]:
        with self.lock:
            ids = [r[0] for r in self.conn.execute(
                "SELECT id FROM works WHERE kind = ? AND status = ? ORDER BY created_at",
                (kind, status)).fetchall()]
        return [w for w in (self.get(i) for i in ids) if w]


_store: _Store | None = None
_store_guard = threading.Lock()


def _the_store() -> _Store:
    global _store
    from .store import data_dir

    path = data_dir() / FILE_NAME
    with _store_guard:
        if _store is None or _store.path != path:
            if _store is not None:
                _store.close()
            _store = _Store(path)
        return _store


# --- one round ----------------------------------------------------------------------------

def scope_for(work_id: str, epoch: int, round_no: int) -> str:
    """The operation scope of one round — no ':' in it, so a tool call's operation id reads
    `<scope>:<name>:<argument hash>#<n>`."""
    return f"{work_id}/e{epoch}/r{round_no}"


def _clear_failures(scope: str) -> int:
    """Forget this round's recorded FAILED low-risk calls, so replaying it tries them again
    rather than handing back the old failure. Successes stay — that is what stops a repeat —
    and so does a failed action that reaches outside Jarvis: a failure there (a timeout, say)
    does not prove nothing happened, so the replay is handed the failure, never a second try."""
    from .db import get_db

    prefix = f"{scope}:"
    rows = get_db().execute(
        "SELECT operation_id, capability FROM operations"
        " WHERE ok = 0 AND substr(operation_id, 1, ?) = ?", (len(prefix), prefix)).fetchall()
    cleared = 0
    for operation_id, capability in ((r[0], r[1]) for r in rows):
        if not is_external(capability):
            get_db().execute("DELETE FROM operations WHERE operation_id = ?", (operation_id,))
            cleared += 1
    return cleared


def is_external(capability: str) -> bool:
    """Whether an action can reach outside Jarvis, from its declared risk: LOW only reads (or
    touches nothing that matters); MEDIUM and HIGH change something. An unknown capability
    counts as external — the guess in the safe direction."""
    from .assembly import get_registry
    from .capabilities import Risk

    spec = get_registry().get(capability)
    return spec is None or spec.risk is not Risk.LOW


def unsure(work_id: str) -> list[dict[str, Any]]:
    """External actions this work STARTED that never recorded a result (in the operations
    table, where every finished call — success or failure — is kept). Each may or may not
    have happened; recovery never repeats one unasked."""
    from .db import get_db

    store = _the_store()
    work = store.get(work_id)
    if work is None:
        return []
    out = []
    for operation_id, capability in store.started_external(work_id, work["epoch"]):
        row = get_db().execute("SELECT 1 FROM operations WHERE operation_id = ?",
                               (operation_id,)).fetchone()
        if row is None:
            out.append({"capability": capability, "operationId": operation_id})
    return out


_cancels: dict[str, threading.Event] = {}
_cancels_lock = threading.Lock()


def _bus() -> Any:
    """The bus the turn loop publishes on (a stand-in orchestrator without one: the default)."""
    from .assembly import get_orchestrator
    from .events import bus as default_bus

    return getattr(get_orchestrator(), "event_bus", None) or default_bus


def _plain(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The working transcript as plain data to save — without the in-memory ids, which the
    restore hands out afresh."""
    return json.loads(json.dumps([{k: v for k, v in m.items() if k != "id"} for m in messages],
                                 default=str))


def _call_id_for(messages: list[dict[str, Any]], capability: str) -> str | None:
    """The call id of the parked call: the last result recorded for that capability."""
    for message in reversed(messages):
        if message.get("role") != "tool":
            continue
        for entry in reversed(message.get("toolResults") or []):
            if entry.get("name") == capability:
                return entry.get("id")
    return None


def run_round(work_id: str, kind_name: str, round_no: int, text: str,
              transcript: list[dict[str, Any]], epoch: int,
              max_steps: int | None = None) -> dict[str, Any]:
    """Run one round's turn and say how it ended. Its return value is what gets saved."""
    from .events import EventType
    from .orchestrator import ApprovalRequired, Done, Failed, Interrupted, ToolRan

    kind = _kind(kind_name)
    session = kind.session_for(work_id)
    conversation.load_snapshot(session, transcript)
    scope = scope_for(work_id, epoch, round_no)
    spec = TurnSpec(turn_id=scope, operation_scope=scope, continuable=kind.continuable,
                    max_steps=max_steps)

    if kind.stopped is not None and kind.stopped(work_id):
        _the_store().record_round(work_id, epoch, round_no, "stopped", 0)
        return {"outcome": "stopped", "text": "", "error": None, "approval": None,
                "steps": 0, "files": [], "transcript": _plain(conversation.get_messages(session))}
    _clear_failures(scope)

    with _cancels_lock:
        cancel = _cancels.setdefault(work_id, threading.Event())

    steps = 0
    bus = _bus()

    def count(event: Any) -> None:
        nonlocal steps
        if event.payload.get("turnId") == spec.turn_id:
            steps += 1

    store = _the_store()

    def started(event: Any) -> None:
        # Written BEFORE the action runs (the bus is synchronous): the write-ahead half of
        # knowing, after a crash, which action may or may not have happened.
        operation = str(event.payload.get("operationId") or "")
        if event.payload.get("sessionId") == session and operation.startswith(f"{scope}:"):
            name = str(event.payload.get("capability") or "")
            store.mark_started(work_id, epoch, operation, name, is_external(name))

    def watch(event: Any) -> None:
        if event.payload.get("sessionId") == session and kind.observe is not None:
            try:
                kind.observe(work_id, event.type.value, dict(event.payload))
            except Exception:  # noqa: BLE001 — a record that fails must not fail the work
                logger.exception("could not record %s for %s", event.type.value, work_id)

    detach = [bus.subscribe(EventType.MODEL_CALL_STARTED, count),
              bus.subscribe(EventType.TOOL_STARTED, started)]
    if kind.observe is not None:
        detach.append(bus.subscribe(None, watch))
    outcome: dict[str, Any] = {"outcome": "failed", "text": "", "error": "It ended without an "
                               "answer.", "approval": None}
    files: list[dict[str, Any]] = []
    try:
        for event in kind.turn(work_id, text, spec, cancel):
            if kind.on_event is not None:
                kind.on_event(work_id, event)
            if isinstance(event, ToolRan) and event.ok:
                # What the round made (a file, an artifact), so a finish can hand it on.
                files.extend(a for a in event.attachments if a not in files)
            if isinstance(event, Done):
                ran_out = kind.continuable and event.final_step
                outcome = {"outcome": "ran_out" if ran_out else "finished", "text": event.text,
                           "error": None, "approval": None, "modelId": event.model_id}
            elif isinstance(event, Failed):
                ran_out = kind.continuable and event.code == "out_of_steps"
                outcome = {"outcome": "ran_out" if ran_out else "failed", "text": "",
                           "error": event.error, "approval": None}
            elif isinstance(event, Interrupted):
                outcome = {"outcome": "stopped", "text": "", "error": None, "approval": None}
            elif isinstance(event, ApprovalRequired):
                outcome = {"outcome": "parked", "text": "", "error": None,
                           "approval": {"id": event.approval_id, "capability": event.capability,
                                        "reason": event.reason}}
    finally:
        for undo in detach:
            undo()

    if outcome["outcome"] == "failed":
        # A call left with no result would make every later request invalid.
        conversation.remove_last_orphaned_tool_call(session)
    messages = conversation.get_messages(session)
    if outcome["approval"] is not None:
        outcome["approval"]["callId"] = _call_id_for(messages, outcome["approval"]["capability"])
    store.record_round(work_id, epoch, round_no, outcome["outcome"], steps)
    return {**outcome, "steps": steps, "files": json.loads(json.dumps(files, default=str)),
            "transcript": _plain(messages)}


def settle(transcript: list[dict[str, Any]], approval: dict[str, Any],
           answer: dict[str, Any]) -> list[dict[str, Any]]:
    """The saved transcript with a parked call's "needs your go-ahead" replaced by what really
    happened once the person answered — so the next round does not believe it never ran."""
    result = answer.get("result") if answer.get("allowed") else {"error": DECLINED}
    out = json.loads(json.dumps(transcript, default=str))
    for message in reversed(out):
        if message.get("role") != "tool":
            continue
        for entry in message.get("toolResults") or []:
            if entry.get("id") == approval.get("callId") and \
                    entry.get("name") == approval.get("capability"):
                entry["result"] = result
                return out
    return out


def continuation(answer: dict[str, Any] | None, goal: str = "") -> str:
    """What the next round is told: carry on with the work (restated — the working transcript
    keeps only its newest messages, so after a few rounds the goal itself may have scrolled
    out), plus what the person said (`guidance`) or why it is being picked back up (`note` —
    a retry after a stall)."""
    answer = answer or {}
    text = CONTINUE_TEXT
    if goal.strip():
        text += f"\n\nThe work you are doing: {goal.strip()}"
    guidance = str(answer.get("guidance") or "").strip()
    note = str(answer.get("note") or "").strip()
    if note:
        text += f"\n\nThe last round did not get anywhere: {note} Try a different approach."
    if guidance:
        text += f"\n\nGuidance from the person: {guidance}"
    return text


# --- the workflow -----------------------------------------------------------------------------

def _build_workflow(saver: Any) -> Any:
    from langgraph.func import entrypoint, task

    @task
    def round_task(work_id: str, kind_name: str, round_no: int, text: str,
                   transcript: list[dict[str, Any]], epoch: int,
                   max_steps: int | None) -> dict[str, Any]:
        return run_round(work_id, kind_name, round_no, text, transcript, epoch, max_steps)

    @task
    def park_task(work_id: str, kind_name: str, why: dict[str, Any]) -> bool:
        _kind(kind_name).park(work_id, why)
        return True

    @task
    def finish_task(work_id: str, kind_name: str, outcome: dict[str, Any]) -> Any:
        return _kind(kind_name).finish(work_id, {k: v for k, v in outcome.items()
                                                 if k != "transcript"})

    @entrypoint(checkpointer=saver)
    def workflow(inp: dict[str, Any], *, previous: Any = None) -> Any:
        """One RUN: from the start (or from where the last run stopped to wait, when `inp`
        carries the person's `answer`) until the work finishes or has to wait again.

        A wait ENDS the run, saving where it stands (`previous` of the next run) — it is not a
        LangGraph `interrupt()`. Found by a real restart: a run picked back up after a crash
        hands an EARLIER answer to a later `interrupt()`, so recovery could have answered a
        question on the person's behalf. Ending the run instead means picking up a finished
        run runs nothing, and only an explicit answer ever starts the next one."""
        work_id, kind_name, epoch = inp["id"], inp["kind"], inp["epoch"]
        continuable = _kind(kind_name).continuable
        budget = inp.get("budget")
        saved = (previous or {}).get("state") if inp.get("answer") is not None else None
        if saved:
            transcript, round_no = saved["transcript"], saved["round_no"]
            spent, allowance, why = saved["spent"], saved["allowance"], saved["why"]
            answer = inp["answer"]
            if why["reason"] == "approval" and isinstance(answer.get("approval"), dict):
                transcript = settle(transcript, why["approval"], answer["approval"])
            if why["reason"] == "budget" and budget:
                # "Keep going" grants another full allowance from where it stands.
                allowance = spent + budget
            text = continuation(answer, inp["text"])
        else:
            text, transcript, round_no, spent, allowance = inp["text"], [], 0, 0, budget

        while True:
            left = None if allowance is None else max(1, allowance - spent)
            out = round_task(work_id, kind_name, round_no, text, transcript, epoch,
                             left).result()
            round_no += 1
            spent += int(out.get("steps") or 0)
            transcript = out["transcript"]
            if not continuable:
                return entrypoint.final(
                    value={"finished": finish_task(work_id, kind_name, out).result()},
                    save={"state": None})

            if out["outcome"] == "finished":
                verdict = finish_task(work_id, kind_name, out).result()
                if not (isinstance(verdict, dict) and verdict.get("accepted") is False):
                    return entrypoint.final(value={"finished": verdict}, save={"state": None})
                why = {"reason": "rejected", "error": verdict.get("error"), "steps": spent}
            elif out["outcome"] == "ran_out":
                if allowance is None or spent < allowance:
                    text = continuation(None, inp["text"])
                    continue
                why = {"reason": "budget", "steps": spent}
            elif out["outcome"] == "parked":
                why = {"reason": "approval", "approval": out["approval"], "steps": spent}
            elif out["outcome"] == "stopped":
                why = {"reason": "stopped", "steps": spent}
            else:
                why = {"reason": "failed", "error": out.get("error"), "steps": spent}

            park_task(work_id, kind_name, why).result()
            return entrypoint.final(
                value={"waiting": why},
                save={"state": {"transcript": transcript, "round_no": round_no, "spent": spent,
                                "allowance": allowance, "why": why}})

    return workflow


# --- driving it -------------------------------------------------------------------------------

_work_locks: dict[str, threading.Lock] = {}
_work_locks_guard = threading.Lock()


def _lock_for(work_id: str) -> threading.Lock:
    with _work_locks_guard:
        return _work_locks.setdefault(work_id, threading.Lock())


def _thread(work_id: str, epoch: int) -> dict[str, Any]:
    return {"configurable": {"thread_id": f"{work_id}#{epoch}"}}


def _invoke(store: _Store, work: dict[str, Any], value: Any) -> dict[str, Any]:
    """Run the workflow until it finishes or waits, and record which."""
    store.set(work["id"], status="running", waiting_on=None)
    with _cancels_lock:
        _cancels[work["id"]] = threading.Event()
    try:
        result = store.workflow.invoke(value, _thread(work["id"], work["epoch"]),
                                       durability="sync")
    finally:
        with _cancels_lock:
            _cancels.pop(work["id"], None)
    if isinstance(result, dict) and "waiting" in result:
        store.set(work["id"], status="waiting", waiting_on=result["waiting"])
        return {"state": "waiting", "why": result["waiting"]}
    finished = result.get("finished") if isinstance(result, dict) else result
    store.set(work["id"], status="finished", result=finished)
    return {"state": "finished", "result": finished}


def advance(work_id: str, kind: str, text: str, *, budget: int | None = None,
            answer: dict[str, Any] | None = None,
            meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Move a piece of work forward, whatever state it is in — the one call every path uses.

    Never started (or forgotten) → start it. Interrupted mid-round by a crash → pick it up at
    that round. Waiting on someone → continue it with `answer` (guidance, an approval's
    outcome, or `{}`: "carry on"); with no answer at all it stays waiting, so starting or
    recovering work can never answer a question on the person's behalf. Finished → nothing
    runs; the recorded result is returned. `meta` is the kind's own plain data, kept with the
    work from its first start (`get(work_id)["meta"]`).
    Blocks while another thread is advancing the same work, so it never runs twice at once.
    """
    _kind(kind)
    with _lock_for(work_id):
        store = _the_store()
        work = store.get(work_id)
        if work is None:
            store.insert(work_id, kind, text, budget, meta)
            work = store.get(work_id)
            assert work is not None
            return _invoke(store, work, _input(work))
        if work["status"] == "new":
            store.set(work_id, text=text, budget=budget)
            work = store.get(work_id)
            assert work is not None
            return _invoke(store, work, _input(work))
        if work["status"] == "waiting":
            if answer is None:
                return {"state": "waiting", "why": work["waiting_on"]}
            return _invoke(store, work, {**_input(work), "answer": _resume_value(answer)})
        if work["status"] == "finished":
            return {"state": "finished", "result": work["result"]}
        # Interrupted mid-round by a crash: pick that round up. An answer given meanwhile
        # (the crash came just as it was about to wait) applies once it is waiting again.
        reached = _invoke(store, work, None if _has_checkpoint(store, work) else _input(work))
        if reached["state"] == "waiting" and answer is not None:
            work = store.get(work_id)
            assert work is not None
            return _invoke(store, work, {**_input(work), "answer": _resume_value(answer)})
        return reached


def recover(work_id: str, *, max_recoveries: int | None = None) -> dict[str, Any] | None:
    """After a restart: continue work that was interrupted mid-round. Work that is waiting
    on someone, or finished, is left exactly as it is (None).

    Never repeats an action that may have happened: if an external action started and never
    recorded a result, nothing runs and `{"state": "unsure", "actions": [...]}` says which —
    the work stays where it is until someone decides (`advance` then continues it). With
    `max_recoveries`, work already recovered that many times is not tried again
    (`{"state": "abandoned"}`)."""
    with _lock_for(work_id):
        store = _the_store()
        work = store.get(work_id)
        if work is None or work["status"] != "running":
            return None
        actions = unsure(work_id)
        if actions:
            return {"state": "unsure", "actions": actions}
        if max_recoveries is not None and work["recoveries"] >= max_recoveries:
            return {"state": "abandoned", "recoveries": work["recoveries"]}
        store.set(work_id, recoveries=work["recoveries"] + 1)
        work = store.get(work_id)
        assert work is not None
        return _invoke(store, work, None if _has_checkpoint(store, work) else _input(work))


def forget(work_id: str) -> None:
    """Start over next time: drop the saved rounds. The next epoch's operation ids differ, so
    actions the old attempt finished are genuinely done again — that is what a restart is."""
    stop(work_id)
    with _lock_for(work_id):
        store = _the_store()
        work = store.get(work_id)
        if work is None:
            return
        store.saver.delete_thread(_thread(work_id, work["epoch"])["configurable"]["thread_id"])
        store.set(work_id, epoch=work["epoch"] + 1, status="new", waiting_on=None, result=None)


def abandon(work_id: str, result: Any = None) -> None:
    """Mark work finished without running anything more — it will not be recovered again."""
    with _lock_for(work_id):
        store = _the_store()
        if store.get(work_id) is not None:
            store.set(work_id, status="finished", waiting_on=None, result=result)


def stop(work_id: str) -> bool:
    """Interrupt the round in flight, if any. The work then waits (`why.reason == 'stopped'`)."""
    with _cancels_lock:
        cancel = _cancels.get(work_id)
    if cancel is None:
        return False
    cancel.set()
    return True


def get(work_id: str) -> dict[str, Any] | None:
    """The work's own record: kind, status (new/running/waiting/finished), what it waits on,
    how often it was recovered, the kind's `meta`."""
    return _the_store().get(work_id)


def peek(work_id: str) -> dict[str, Any] | None:
    """`get`, without creating the checkpoint file when there has never been any durable
    work — for a startup sweep that only needs to know whether something is durable."""
    from .store import data_dir

    if _store is None and not (data_dir() / FILE_NAME).exists():
        return None
    return get(work_id)


def with_status(kind: str, status: str) -> list[dict[str, Any]]:
    return _the_store().by_status(kind, status)


def rounds(work_id: str) -> list[dict[str, Any]]:
    """Every round the work has finished in its current attempt, oldest first: number, how it
    ended, model steps. Written as each round ends, so a round replayed after a crash appears
    once, with how it ended the time it actually finished."""
    store = _the_store()
    work = store.get(work_id)
    if work is None:
        return []
    with store.lock:
        rows = store.conn.execute(
            "SELECT round_no, outcome, steps, finished_at FROM rounds"
            " WHERE work_id = ? AND epoch = ? ORDER BY round_no",
            (work_id, work["epoch"])).fetchall()
    return [{"round": r[0], "outcome": r[1], "steps": r[2], "finishedAt": r[3]} for r in rows]


def _resume_value(answer: dict[str, Any]) -> dict[str, Any]:
    """What a wait is answered with — never empty, so "carry on" with nothing more to say is
    still unmistakably an answer."""
    return {"answered": True, **(answer or {})}


def _input(work: dict[str, Any]) -> dict[str, Any]:
    return {"id": work["id"], "kind": work["kind"], "text": work["text"],
            "budget": work["budget"], "epoch": work["epoch"]}


def _has_checkpoint(store: _Store, work: dict[str, Any]) -> bool:
    return store.saver.get_tuple(_thread(work["id"], work["epoch"])) is not None


def reset_for_tests() -> None:
    """Test-only: close the checkpoint file and forget in-process state."""
    global _store
    with _store_guard:
        if _store is not None:
            _store.close()
        _store = None
    with _cancels_lock:
        _cancels.clear()
    with _work_locks_guard:
        _work_locks.clear()
