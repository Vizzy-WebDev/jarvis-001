"""Running one job's work, on its own session.

**A worker structurally cannot write into the conversation the user is looking
at.** Its session id is `job:<id>`, sessions are keyed separately, and a worker
session is never bound to chat history — so its working turns never appear in the
conversation list and never share a transcript with what the user is actually
talking about. That is a property of the wiring, not a convention to remember.

**A worker never asks the user anything directly.** It runs with
`Autonomy.PRE_CONSENTED` — the person set the work going, so ordinary steps toward its goal
are already theirs, exactly like a scheduled task — and a HIGH-risk call needing a human
parks into `awaiting_decision` with an outbox row rather than minting a confirmation nobody
is present to answer. A job may wait hours; a short-lived token would be long expired.

Every effectful step is traced before and after, which is what makes recovery a
reading of the record rather than a guess.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Iterator

from ..events import EventType, bus as default_bus
from ..events.bus import EventBus
from ..jscompat import now_iso
from ..policy import Autonomy, Surface
from . import job_store
from .policy import STEP_BUDGET_BY_KIND

logger = logging.getLogger(__name__)

#: Tool sets per kind. `generic` is deliberately the FULL catalogue with no
#: fence — it is not "no expertise", it is the opposite. The named kinds are
#: small hardcoded lists chosen for work whose shape is known.
#: A worker's own tools, added to every restricted kind. A narrow, repetitive
#: job is exactly the one most likely to turn out to be three jobs, so leaving
#: these out of the fenced kinds would let only unrestricted work ask to split.
JOB_OWN_TOOLS = ["request_job_split"]

TOOLS_BY_KIND: dict[str, list[str] | None] = {
    "generic": None,
    "research": ["look_it_up", "read_web_page", "get_headlines", *JOB_OWN_TOOLS],
    "files": ["read_web_page", *JOB_OWN_TOOLS],
}


def session_for(job_id: str) -> str:
    return f"job:{job_id}"


def _installed_skill_names() -> list[str]:
    """Every Skill currently installed.

    A `research`/`files`-kind job must still be able to reach an installed Skill however
    narrow its kind's own tool list is: a Skill is the user's own packaged process, not a raw
    capability the kind restriction exists to fence off.

    A lazy import, same reason `run_job()` below defers its own `assembly`
    import: the registry is built by `assembly.py`, which this module must
    stay safe to import without pulling in at module load time."""
    from ..assembly import get_registry
    from ..capabilities import CapabilityKind

    return [spec.name for spec in get_registry().list(kind=CapabilityKind.SKILL)]


#: The bus each job's own events go to, for the hooks the durable runner calls on its
#: behalf (they run inside saved steps, so the bus cannot travel as an argument).
_buses: dict[str, EventBus] = {}


def _bus_for(job_id: str) -> EventBus:
    return _buses.get(job_id) or default_bus


def run_job(job_id: str, *, event_bus: EventBus | None = None,
            answer: dict[str, Any] | None = None, fresh: bool = False) -> dict[str, Any]:
    """Move one job forward — to a conclusion, or to the point where a person is needed.

    The job is durable work (`jarvis/durable.py`): it runs in rounds, each an ordinary turn,
    and every finished round is saved. Started, resumed after a decision, retried after a
    stall or picked up after a restart, it is this same call — the runner knows which, and
    never repeats a finished round. `answer` is what it is continued with (guidance, an
    approval's outcome); `fresh` starts it over from the goal (a restart).
    """
    from .. import durable

    ebus = event_bus or default_bus
    job = job_store.get_job(job_id)
    if job is None:
        raise KeyError(f"Unknown job: {job_id}")
    if fresh:
        durable.forget(job_id)
    else:
        work = durable.get(job_id)
        settled = (work or {}).get("status")
        if settled == "finished" or (settled == "waiting" and answer is None):
            # Nothing to run: it already finished, or it is waiting on an answer that a
            # plain start or recovery must never supply on the person's behalf. If the job's
            # own row fell out of step (a crash between the two records), it is put back.
            report = _report(work or {}, job_id)
            if job.get("status") in ("queued", "running"):
                job_store.update_job(job_id, {"status": _status_of(report)})
            return report

    _buses[job_id] = ebus
    job_store.update_job(job_id, {"status": "running", "startedAt": job.get("startedAt") or now_iso()})
    job_store.heartbeat(job_id, step="starting")
    ebus.publish(EventType.JOB_UPDATED, {"id": job_id, "status": "running"})

    budget = STEP_BUDGET_BY_KIND.get(job["kind"], STEP_BUDGET_BY_KIND["generic"])
    try:
        reached = durable.advance(job_id, KIND, job["goal"], budget=budget, answer=answer)
    finally:
        _buses.pop(job_id, None)
    report = _report({"status": reached["state"], "result": reached.get("result"),
                      "waiting_on": reached.get("why")}, job_id)
    if (job_store.get_job(job_id) or {}).get("status") == "running":
        # Its hooks normally say where it stopped; a pause re-reached from a saved step
        # (after a crash) runs no hook again, so the row is set from the record.
        job_store.update_job(job_id, {"status": _status_of(report), "currentStep": None})
    return report


def _status_of(report: dict[str, Any]) -> str:
    status = report.get("status")
    return status if status in ("done", "awaiting_decision", "stalled", "cancelled") \
        else "awaiting_decision"


def _report(work: dict[str, Any], job_id: str | None = None) -> dict[str, Any]:
    """Where the job's durable work got to, in the shape a caller of `run_job` reads."""
    if work.get("status") == "finished":
        result = work.get("result")
        return result if isinstance(result, dict) else {"status": "done"}
    why = work.get("waiting_on") or {}
    reason = why.get("reason")
    if reason == "approval":
        return {"status": "awaiting_decision", "approvalId": (why.get("approval") or {}).get("id")}
    if reason == "budget":
        return {"status": "awaiting_decision", "reason": "budget", "steps": why.get("steps")}
    if reason in ("failed", "rejected"):
        return {"status": "stalled", "error": _stall_detail(why), "cause": _stall_cause(why)}
    job = job_store.get_job(job_id or work.get("id") or "") or {}
    return {"status": job.get("status"), "reason": reason}


# --- the job kind of durable work --------------------------------------------

KIND = "job"


def _allowed_for(job: dict[str, Any]) -> frozenset[str] | None:
    allowed = TOOLS_BY_KIND.get(job["kind"], None)
    if allowed is None:
        return None
    # A restricted kind's own fence was never meant to keep out a Skill —
    # see _installed_skill_names()'s own header comment.
    return frozenset([*allowed, *_installed_skill_names()])


def _turn(job_id: str, text: str, spec: Any, cancel: threading.Event) -> Iterator[Any]:
    """One round's turn: an ordinary turn on the job's own session, nobody present."""
    from ..assembly import get_orchestrator
    from ..orchestrator import Failed, TurnRequest

    job = job_store.get_job(job_id) or {}
    job_store.heartbeat(job_id, step="working")
    if job.get("agentId"):
        # A specialist's job: the same turn, run AS that agent through the one
        # agent path, so its doctrine, access and model apply and the run is
        # recorded like any other. Supervision is unchanged.
        from ..agents import runner

        try:
            agent = runner.usable_agent(job["agentId"])
        except runner.AgentUnavailable as err:
            yield Failed(str(err), code="specialist_unavailable")
            return
        run = runner.start_run(agent, text, session_id=session_for(job_id),
                               requested_by="job", conversation_id=job.get("conversationId"),
                               job_id=job_id, event_bus=_bus_for(job_id))
        yield from runner.stream_run(agent, run, text, autonomy=Autonomy.ESCALATE,
                                     surface=Surface.SCHEDULED, event_bus=_bus_for(job_id),
                                     cancel=cancel, turn=spec)
        return
    allowed = _allowed_for(job)
    request = TurnRequest(
        text=text,
        session_id=session_for(job_id),
        surface=Surface.JOB,
        # The person set this work going (their own request, the Jobs screen or an approved
        # split), so ordinary steps toward its goal are theirs already: the same "set up in
        # advance" rule a scheduled task runs under. A HIGH-risk step still parks — nobody is
        # present to ask — and a computer job is still created parked.
        autonomy=Autonomy.PRE_CONSENTED,
        turn_id=spec.turn_id,
        allowed_names=allowed,
        operation_scope=spec.operation_scope,
        continuable=spec.continuable,
        max_steps=spec.max_steps,
    )
    yield from get_orchestrator().run_turn(request, cancel)


def _on_event(job_id: str, event: Any) -> None:
    from ..orchestrator import ToolRan

    if isinstance(event, ToolRan):
        job_store.heartbeat(job_id, step=f"using {event.capability}")


def _effect_of(capability: str) -> str:
    """What an action can touch, from its declared risk (`durable.is_external`): LOW only
    reads; MEDIUM and HIGH change something that may be outside Jarvis."""
    from .. import durable

    return "external" if durable.is_external(capability) else "read"


def _observe(job_id: str, event_type: str, payload: dict[str, Any]) -> None:
    """The write-ahead trace, from what really happened: an `intent` row BEFORE an action
    runs and an `outcome` row after it, both carrying the operation id that ties them."""
    name = str(payload.get("capability") or "")
    if event_type == EventType.TOOL_STARTED.value:
        job_store.append_trace(job_id, phase="intent", effect=_effect_of(name), kind="tool",
                               summary=f"{name} starting",
                               detail={"name": name, "args": payload.get("args") or {},
                                       "operationId": payload.get("operationId")})
    elif event_type in (EventType.TOOL_COMPLETED.value, EventType.TOOL_FAILED.value):
        ok = event_type == EventType.TOOL_COMPLETED.value
        job_store.append_trace(job_id, phase="outcome", effect=_effect_of(name), kind="tool",
                               summary=f"{name} {'completed' if ok else 'failed'}",
                               detail={"name": name, "ok": ok, "error": payload.get("error"),
                                       "operationId": payload.get("operationId")})


def _stopped(job_id: str) -> bool:
    return (job_store.get_job(job_id) or {}).get("status") == "cancelled"


def _finish(job_id: str, outcome: dict[str, Any]) -> dict[str, Any]:
    """The work says it is finished: check that, then record it once."""
    job = job_store.get_job(job_id) or {}
    answer = outcome.get("text") or ""
    # "It finished" is not the same as "it did what was asked". A checked
    # mismatch is treated exactly like a stall — the SAME single retry, the same
    # counter, the same escalation — rather than a second recovery mechanism
    # with its own rules. An unchecked one changes nothing.
    verdict = _verify_result(job, answer)
    if verdict is not None and verdict.failed:
        return {"accepted": False,
                "error": "it finished, but it did not do what was asked: "
                         f"{verdict.reason or 'the result does not match the request'}"}

    from .. import durable

    job_store.update_job(job_id, {"status": "done", "result": answer,
                                  "finishedAt": now_iso(), "progress": 100,
                                  "currentStep": None, "error": None})
    # Once per attempt: a finish replayed after a crash records nothing twice, and a job
    # started over (a restart) that finishes again says so again.
    attempt = int((durable.get(job_id) or {}).get("epoch") or 0) + 1
    note = "finished" if attempt == 1 else f"finished (attempt {attempt})"
    if not _has_trace(job_id, note):
        job_store.append_trace(job_id, phase="outcome", effect="read", kind="note",
                               summary=note, detail=answer[:500])
    _bus_for(job_id).publish(EventType.JOB_COMPLETED, {"id": job_id, "status": "done",
                                                       "title": job.get("title")})
    _deliver_result(job_id, job, answer, attempt, outcome.get("files") or [])
    return {"accepted": True, "status": "done", "result": answer}


#: A finished result this short is said whole when the person is here; longer, they are told
#: it is done and the full result waits for the next thing they say.
SPOKEN_RESULT_CHARS = 800


def _deliver_result(job_id: str, job: dict[str, Any], answer: str, attempt: int,
                    files: list[dict[str, Any]]) -> None:
    """Get the finished result to the person — the follow-through a promise to "get back to
    you" needs. The same three ways a late specialist result arrives: a notice carrying the
    whole result (shown on the next turn they start), a notification, and — when they are
    actually here — Jarvis saying so first. Each is safe to replay after a crash."""
    title = str(job.get("title") or "The job")
    speaking = _may_speak()
    entry = _outbox_once(
        job_id, tier=2, reason="finished", summary=f'"{title}" finished',
        detail={"result": answer, "attempt": attempt, "jobId": job_id, "files": files,
                "conversationId": job.get("conversationId"), "announced": speaking},
        ignore=("announced",))
    if entry is None:
        return  # already delivered by an earlier run of this finish
    try:
        from .. import notifications

        notifications.add(kind="job", level="success", title=f'"{title}" finished',
                          body=answer[:300],
                          action={"label": "Background Jobs", "section": "jobs"},
                          meta={"jobId": job_id}, event_bus=_bus_for(job_id))
    except Exception:  # noqa: BLE001 — the result is recorded either way
        logger.exception("could not notify about job %s", job_id)
    if speaking:
        whole = bool(answer) and len(answer) <= SPOKEN_RESULT_CHARS
        text = f'"{title}" is done. {answer}' if whole else \
            f'"{title}" is done — I have the full result when you want it.'
        _say(text, entry if whole else None, "A background job finished.", job_id)


def _may_speak() -> bool:
    try:
        from ..heartbeat.speak import may_speak_now

        return may_speak_now()
    except Exception:  # noqa: BLE001 — not knowing means not interrupting
        logger.exception("could not tell whether the person is here")
        return False


def _say(text: str, entry_id: int | None, reason: str, job_id: str) -> None:
    """Jarvis speaks first (`heartbeat/speak.py`). Marks the row delivered only when saying
    it really was the whole message. Speaking up first is exactly where a right hand uses the
    form of address, so the line opens with it (`prompt.IDENTITY`)."""
    try:
        from ..heartbeat.speak import speak_now

        speak_now(f"Boss, {text}", outbox_id=entry_id, reason=reason, event_bus=_bus_for(job_id))
    except Exception:  # noqa: BLE001 — the row stays waiting for the next turn instead
        logger.exception("could not say aloud what job %s needs", job_id)


def _ask_aloud(job_id: str, entry_id: int | None, text: str, reason: str) -> None:
    """A question the job cannot continue without: if the person is here, ask it now."""
    if entry_id is not None and _may_speak():
        _say(text, entry_id, reason, job_id)


def _park(job_id: str, why: dict[str, Any]) -> None:
    """The work has to wait — record why, for the person and for the supervisor."""
    reason = why.get("reason")
    if reason == "approval":
        _park_for_approval(job_id, why.get("approval") or {})
    elif reason == "budget":
        _park_for_budget(job_id, int(why.get("steps") or 0))
    elif reason in ("failed", "rejected"):
        _stall(job_id, _stall_detail(why), _bus_for(job_id), cause=_stall_cause(why))
    # "stopped": whoever stopped it (the person cancelling, the supervisor) already said why.


def _stall_detail(why: dict[str, Any]) -> str:
    return str(why.get("error") or "it stopped making progress")


def _stall_cause(why: dict[str, Any]) -> str:
    return "did not match the request" if why.get("reason") == "rejected" else "failed"


def _park_for_approval(job_id: str, approval: dict[str, Any]) -> None:
    job = job_store.get_job(job_id) or {}
    capability = approval.get("capability") or "something"
    job_store.update_job(job_id, {"status": "awaiting_decision",
                                  "currentStep": f"waiting on {capability}"})
    if not any(row.get("kind") == "decision" and approval.get("id")
               and str(approval.get("id")) in str(row.get("detail") or "")
               for row in job_store.get_trace(job_id)):
        job_store.append_trace(job_id, phase="intent", effect="external", kind="decision",
                               summary=f"{capability} needs a decision",
                               detail={"approvalId": approval.get("id")})
    # Tier 1: the job cannot continue without an answer, so this is worth
    # raising the next time the user is here.
    summary = f'"{job.get("title")}" needs your go-ahead: {approval.get("reason") or ""}'
    entry = _outbox_once(job_id, tier=1, reason="permission", summary=summary,
                         detail={"approvalId": approval.get("id"), "capability": capability})
    _bus_for(job_id).publish(EventType.JOB_UPDATED, {"id": job_id, "status": "awaiting_decision"})
    _ask_aloud(job_id, entry, summary, "A background job needs a go-ahead.")


def _park_for_budget(job_id: str, steps: int) -> None:
    job = job_store.get_job(job_id) or {}
    job_store.update_job(job_id, {"status": "awaiting_decision", "currentStep": None})
    summary = f'"{job.get("title")}" is still not done after {steps} steps. Keep going?'
    entry = _outbox_once(job_id, tier=1, reason="budget", summary=summary,
                         detail={"steps": steps})
    _bus_for(job_id).publish(EventType.JOB_UPDATED, {"id": job_id, "status": "awaiting_decision"})
    _ask_aloud(job_id, entry, summary, "A background job is out of steps.")


def _has_trace(job_id: str, summary: str) -> bool:
    return any(row.get("summary") == summary for row in job_store.get_trace(job_id))


def _outbox_once(job_id: str, *, tier: int, reason: str, summary: str, detail: Any = None,
                 ignore: tuple[str, ...] = ()) -> int | None:
    """Add an outbox row unless the same one was already raised — a step replayed after a
    crash must not ask the person the same thing twice, nor ask again what they already
    answered (an answered row is delivered, and still the same question). Returns the new
    row's id, or None when it already existed. `ignore` names detail keys that may differ
    between a first run and its replay (whether the person was here to hear it)."""
    from ..heartbeat import outbox

    def keyed(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, dict) and ignore:
            value = {k: v for k, v in value.items() if k not in ignore}
        return json.dumps(value, sort_keys=True, default=str)

    wanted = keyed(detail)
    for row in outbox.for_job(job_id):
        if row["reason"] == reason and row["summary"] == summary and keyed(row["detail"]) == wanted:
            return None
    return job_store.add_outbox(tier=tier, job_id=job_id, reason=reason, summary=summary,
                                detail=detail)


def _verify_result(job: dict[str, Any], answer: str) -> Any:
    """One budgeted semantic check on a finished job. None when it did not run.

    A job is worth the call in a way a chat reply usually is not: nobody watched
    it happen, and its result is reported later as fact.
    """
    if not answer:
        return None
    from ..ops.verify import verify_semantic_match

    try:
        return verify_semantic_match(request=job.get("goal") or job.get("title") or "",
                                     result_summary=f'the background job "{job.get("title")}"',
                                     result_text=answer)
    except Exception:  # noqa: BLE001 — a check that fails must not fail the job
        logger.exception("the semantic check on job %s failed", job.get("id"))
        return None


def _stall(job_id: str, detail: str, ebus: EventBus, cause: str = "failed") -> dict[str, Any]:
    """A job that got stuck. The orchestrator decides whether to spend its one
    retry — this only records what happened, so that decision is made from a
    record rather than from whatever the worker felt like reporting."""
    job_store.update_job(job_id, {"status": "stalled", "error": detail})
    job_store.append_trace(job_id, phase="outcome", effect="read", kind="note",
                           summary=f"stalled: {cause}", detail=detail)
    ebus.publish(EventType.JOB_UPDATED, {"id": job_id, "status": "stalled",
                                         "error": detail})
    return {"status": "stalled", "error": detail, "cause": cause}


#: Live worker threads. Tracked rather than fire-and-forgotten so shutdown can
#: wait for them: a worker still writing to the database while the process tears
#: it down is a segfault, not an exception — found exactly that way, by a test
#: closing the connection under a running job.
_threads: list[threading.Thread] = []
_threads_lock = threading.Lock()


def run_in_background(job_id: str, event_bus: EventBus | None = None, *,
                      answer: dict[str, Any] | None = None,
                      fresh: bool = False) -> threading.Thread:
    def _go() -> None:
        try:
            run_job(job_id, event_bus=event_bus, answer=answer, fresh=fresh)
        except Exception:  # noqa: BLE001 — a worker crash must not take the app down
            logger.exception("job %s crashed", job_id)
            try:
                job_store.update_job(job_id, {"status": "stalled", "error": "The work crashed."})
            except Exception:  # noqa: BLE001 — the database may already be gone
                logger.warning("could not record the crash of job %s", job_id)

    thread = threading.Thread(target=_go, name=f"job-{job_id}", daemon=True)
    with _threads_lock:
        _threads[:] = [t for t in _threads if t.is_alive()]
        _threads.append(thread)
    thread.start()
    return thread


def join_all(timeout: float = 10.0) -> bool:
    """Wait for every running worker. Returns whether they all finished.

    Called at shutdown and between tests. A daemon thread is killed abruptly at
    process exit, which for one holding a database write is how a clean stop
    becomes a corrupt file.
    """
    with _threads_lock:
        alive = [t for t in _threads if t.is_alive()]
    for thread in alive:
        thread.join(timeout=timeout)
    return all(not t.is_alive() for t in alive)


def _register() -> None:
    from .. import durable

    durable.register(durable.Kind(
        name=KIND, session_for=session_for, turn=_turn, finish=_finish, park=_park,
        continuable=True, on_event=_on_event, observe=_observe, stopped=_stopped))


_register()
