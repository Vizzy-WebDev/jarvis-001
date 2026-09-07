"""Running scheduled tasks: what is due, running one, and recording what happened.

**The timer is OFF by default and must stay that way until cutover.** The Node app
is still the live one on the user's machine; two schedulers reading the same
`data/tasks.json` would both fire every task, so `start()` does nothing unless
`JARVIS_SCHEDULER=1` is set. That is a deliberate safety interlock, not a
configuration nicety — the failure it prevents is silent and doubles real actions.

**A scheduled task carries pre-consent for ordinary work and never for a
high-risk action.** That is the owner's explicit decision, and it is enforced in
`policy/decide.py` rather than here: agreeing to a task is not agreeing to
whatever it later decides to delete. This module simply runs the turn with
`Autonomy.PRE_CONSENTED`, and a HIGH-risk call inside it parks for a human.

The schedule is advanced BEFORE the run, so a task that throws — or a restart
mid-run — can never get stuck re-firing the same due timestamp forever, and a run
missed for days catches up exactly once.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from datetime import datetime, timedelta
from typing import Any

from ..events import EventType, bus as default_bus
from ..events.bus import EventBus
from ..jscompat import now_iso, to_iso_z
from ..policy import Autonomy, Surface
from .recurrence import next_run_at
from .task_store import get_task, list_tasks, record_run, update_task

logger = logging.getLogger(__name__)

#: Later than this past its due time and the run is reported as late, so the
#: user is told "this was due while Jarvis was closed" rather than silently
#: getting yesterday's reminder as though it were now.
LATE_THRESHOLD = timedelta(minutes=5)

TICK_SECONDS = 30.0

#: The interlock described above.
ENABLE_ENV = "JARVIS_SCHEDULER"


def is_enabled() -> bool:
    return os.environ.get(ENABLE_ENV) == "1"


# --- running one task --------------------------------------------------------

def run_task_now(task_id: str, *, late: bool = False,
                 event_bus: EventBus | None = None) -> dict[str, Any]:
    ebus = event_bus or default_bus
    task = get_task(task_id)
    if task is None:
        raise KeyError(f"Unknown task: {task_id}")

    try:
        result = _run_action(task)
    except Exception as err:  # noqa: BLE001 — a task must never take the loop down
        logger.exception("task %s threw", task_id)
        result = {"ok": False, "summary": "", "error": str(err) or "Something went wrong."}

    # Did the run actually do what the task asked for? Only for a `prompt`
    # action, whose whole output is free text nobody watched being produced.
    # There is no retry loop here to reuse, so a mismatch simply makes the run
    # not-ok with the reason folded in — the next occurrence is already its
    # natural retry, and inventing a second recovery mechanism for it would be
    # a mechanism nobody asked for.
    verdict = _verify_run(task, result)
    if verdict is not None and verdict.failed:
        result = {**result, "ok": False,
                  "error": ((result.get("error") + " ") if result.get("error") else "")
                           + "It ran, but the result did not match what the task asked for: "
                           + (verdict.reason or "no reason given.")}

    update_task(task_id, {"lastRunAt": now_iso()})

    run = record_run({
        "taskId": task_id,
        "title": task["title"],
        "ranAt": now_iso(),
        "late": late,
        "ok": bool(result.get("ok")),
        "summary": result.get("summary") or "",
        "error": result.get("error"),
        "modelId": result.get("modelId"),
        "notify": task.get("notify") or "always",
        # A parked approval is not a failure and not a success: the task did
        # what it could and is waiting for a person.
        "awaitingApproval": result.get("awaitingApproval") or None,
    })
    # Two different things, deliberately: the run always happened and any open
    # screen should see it, but whether the USER is interrupted about it is the
    # task's own notify setting.
    ebus.publish(EventType.JOB_COMPLETED, {"kind": "task_run", **run})
    if _should_notify(run):
        ebus.publish(EventType.NOTIFICATION_CREATED, _notice_for(run))

    # A prompt task's result is free text about the user's own life, which is
    # the one action type that can plausibly surface a new fact worth
    # remembering. Fire-and-forget: a task's own notification must never wait.
    if task["action"].get("type") == "prompt" and run["ok"] and run["summary"]:
        _checkpoint_later(run)

    return result


def _verify_run(task: dict[str, Any], result: dict[str, Any]) -> Any:
    """One budgeted semantic check on a completed prompt task. None if not run."""
    if task["action"].get("type") != "prompt":
        return None
    if not result.get("ok") or not result.get("summary"):
        return None
    from ..ops.verify import verify_semantic_match

    asked = task["action"].get("prompt") or task.get("title") or ""
    try:
        return verify_semantic_match(request=asked,
                                     result_summary=f'the scheduled task "{task.get("title")}"',
                                     result_text=result.get("summary"))
    except Exception:  # noqa: BLE001 — a check must never break the run it checks
        logger.exception("the semantic check on task %s failed", task.get("id"))
        return None


def _should_notify(run: dict[str, Any]) -> bool:
    """'never' stays silent; 'on_error' speaks only when something went wrong;
    anything else (including a task saved before `notify` existed) announces."""
    notify = run.get("notify") or "always"
    if notify == "never":
        return False
    if notify == "on_error" and run["ok"]:
        return False
    return True


def _notice_for(run: dict[str, Any]) -> dict[str, Any]:
    suffix = " (it was due while Jarvis was closed)" if run.get("late") else ""
    if run.get("awaitingApproval"):
        return {"kind": "task_run", "level": "info",
                "title": f'"{run["title"]}" needs your go-ahead{suffix}',
                "body": run.get("error") or "", "meta": {"runId": run["id"]}}
    return {
        "kind": "task_run",
        "level": "success" if run["ok"] else "error",
        "title": (f'"{run["title"]}" ran{suffix}' if run["ok"]
                  else f'"{run["title"]}" ran into a problem{suffix}'),
        "body": "" if run["ok"] else (run.get("error") or "Unknown error."),
        "meta": {"runId": run["id"]},
    }


def _checkpoint_later(run: dict[str, Any]) -> None:
    from ..memory import review

    def _go() -> None:
        try:
            review.checkpoint_from_text(run["summary"], source_kind="task",
                                        source_ref=run["id"])
        except Exception:  # noqa: BLE001
            logger.exception("task-complete checkpoint failed")

    threading.Thread(target=_go, name="task-checkpoint", daemon=True).start()


def _run_action(task: dict[str, Any]) -> dict[str, Any]:
    action = task.get("action") or {}
    kind = action.get("type")

    if kind == "message":
        # Nothing to compute: the point is the reminder itself.
        return {"ok": True, "summary": action.get("text") or task["title"]}

    if kind == "prompt":
        return _run_prompt(task, action)

    if kind == "briefing":
        from .briefing import compose_briefing

        composed = compose_briefing()
        return {"ok": composed.ok, "summary": composed.text, "error": composed.error,
                "modelId": composed.model_id}

    return {"ok": False, "summary": "", "error": f"Unknown task action: {kind}"}


def _run_prompt(task: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
    from ..assembly import get_orchestrator
    from ..orchestrator import ApprovalRequired, Done, Failed, TurnRequest

    text = (action.get("text") or "").strip()
    if not text:
        return {"ok": False, "summary": "", "error": "This task has nothing to ask."}

    # Its own ephemeral session: never bound to chat history, so a task's
    # working turns cannot appear in the user's conversation list, and never
    # shares a transcript with whatever they are actually talking about.
    session_id = f"task:{task['id']}:{uuid.uuid4().hex[:8]}"
    request = TurnRequest(
        text=text,
        session_id=session_id,
        surface=Surface.SCHEDULED,
        # Pre-consent covers ordinary work. A HIGH-risk action inside a
        # scheduled task still parks for a human — the owner's explicit rule,
        # enforced by the policy, not by this module.
        autonomy=Autonomy.PRE_CONSENTED,
        turn_id=uuid.uuid4().hex,
        allowed_names=frozenset(action["tools"]) if action.get("tools") else None,
    )

    answer, parked, failure, model_id = "", None, None, None
    for event in get_orchestrator().run_turn(request):
        if isinstance(event, Done):
            answer = event.text
        elif isinstance(event, ApprovalRequired):
            parked = event
        elif isinstance(event, Failed):
            failure = event

    if parked is not None:
        return {"ok": False, "summary": answer, "awaitingApproval": parked.approval_id,
                "error": f"{parked.capability} needs your go-ahead before this can finish."}
    if failure is not None:
        return {"ok": False, "summary": answer, "error": failure.error}
    return {"ok": True, "summary": answer, "modelId": model_id}


# --- the loop ----------------------------------------------------------------

def due_tasks(now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now()
    out = []
    for task in list_tasks():
        if not task.get("enabled") or not task.get("nextRunAt"):
            continue
        due = _parse(task["nextRunAt"])
        if due is not None and due <= now:
            out.append(task)
    return out


def _parse(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone().replace(tzinfo=None) if parsed.tzinfo else parsed


def tick(now: datetime | None = None, event_bus: EventBus | None = None) -> list[str]:
    """Run everything due. Returns the ids that ran."""
    now = now or datetime.now()
    ran: list[str] = []
    for task in due_tasks(now):
        due = _parse(task["nextRunAt"]) or now
        late = (now - due) > LATE_THRESHOLD

        # Advance BEFORE running — see this module's docstring.
        if (task.get("recurrence") or {}).get("type") == "once":
            update_task(task["id"], {"enabled": False, "nextRunAt": None})
        else:
            update_task(task["id"],
                        {"nextRunAt": to_iso_z(next_run_at(task["recurrence"], now))
                         if next_run_at(task["recurrence"], now) else None})

        try:
            run_task_now(task["id"], late=late, event_bus=event_bus)
        except Exception:  # noqa: BLE001
            logger.exception("task %s failed to run", task["id"])
        ran.append(task["id"])
    return ran


_timer: threading.Thread | None = None
_stop = threading.Event()


def start() -> bool:
    """Start the tick loop, if the interlock allows it. Returns whether it started."""
    global _timer
    if not is_enabled():
        logger.info("[scheduler] not started — set %s=1 to enable "
                    "(the Node app still owns the schedule)", ENABLE_ENV)
        return False
    if _timer is not None and _timer.is_alive():
        return True

    def loop() -> None:
        while not _stop.wait(TICK_SECONDS):
            try:
                tick()
            except Exception:  # noqa: BLE001
                logger.exception("scheduler tick failed")

    _stop.clear()
    _timer = threading.Thread(target=loop, name="scheduler", daemon=True)
    _timer.start()
    logger.info("[scheduler] started")
    return True


def stop() -> None:
    _stop.set()
