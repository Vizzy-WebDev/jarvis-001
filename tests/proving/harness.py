"""Running one scenario through the real Jarvis, and judging it from what it left behind.

`play()` does what a person and a running app do between them: builds the world, says the
scenario's words in a fresh chat through the real turn loop, keeps the job supervisor's clock
going, and waits until nothing is running any more (or the time is up). It is used in-process
by the scripted tests (with a stand-in model) and by `child.py`, the real-model process that
`live.py` starts — and, for a scenario that kills the process, starts twice.

`judge()` then reads the run's folder (`score.collect`) and says how it went.

Clocks are shortened, never skipped: the supervisor's pass and the wait before asking a model
again run through their real code at a few seconds instead of a minute, so a ten-minute outage
is proved by a forty-second one.
"""

from __future__ import annotations

import json
import random
import threading
import time
from pathlib import Path
from typing import Any

from . import score, world
from .scenarios import Fault, Scenario

#: Shortened clocks for a run (seconds): how often the supervisor looks, how long work waits
#: before asking a model again.
SUPERVISOR_TICK_S = 3.0
MODEL_WAITS_S = (12,)
KILL_MARK = "PROVING-KILL-NOW"


class Outage:
    """The model client, with every model switched off for a while for background work —
    failing exactly as the real client does when nothing can answer."""

    def __init__(self, inner: Any, fault: Fault, prefix: str = "job:") -> None:
        self._inner, self._fault, self._prefix = inner, fault, prefix
        self._answered = 0
        self._since: float | None = None
        self.refused = 0
        self._lock = threading.Lock()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def stream(self, **kw: Any) -> Any:
        from jarvis.orchestrator.model_port import ModelUnavailable

        if str(kw.get("session_id") or "").startswith(self._prefix):
            with self._lock:
                if self._since is None and self._answered >= self._fault.after_calls:
                    self._since = time.monotonic()
                down = self._since is not None and time.monotonic() - self._since < self._fault.seconds
                if down:
                    self.refused += 1
                else:
                    self._answered += 1
            if down:
                raise ModelUnavailable(
                    "No model could answer: the proving run switched them all off for a while.",
                    detail={"reason": "unavailable", "scope": "endpoint", "recovery": "after",
                            "retryAfter": None})
        return self._inner.stream(**kw)


def _shorten_clocks() -> None:
    from jarvis import durable
    from jarvis.jobs import orchestrator

    orchestrator.TICK_SECONDS = SUPERVISOR_TICK_S
    durable.MODEL_WAITS_S = MODEL_WAITS_S


def _count_events() -> tuple[dict[str, int], Any]:
    """Count what the turn loop does, across every session, for the run's own record."""
    from jarvis.events import EventType, bus

    counts = {"modelCallStarted": 0, "modelCallCompleted": 0, "modelCallFailed": 0,
              "backgroundCallsAnswered": 0}

    def watch(event: Any) -> None:
        kind = event.type
        session = str(event.payload.get("sessionId") or "")
        if kind is EventType.MODEL_CALL_STARTED:
            counts["modelCallStarted"] += 1
        elif kind is EventType.MODEL_CALL_COMPLETED and "step" in event.payload:
            counts["modelCallCompleted"] += 1
            if session.startswith("job:"):
                counts["backgroundCallsAnswered"] += 1
        elif kind is EventType.MODEL_CALL_FAILED:
            counts["modelCallFailed"] += 1

    return counts, bus.subscribe(None, watch)


def _say(chat: str, said: str) -> dict[str, Any]:
    """One thing said in the chat, through the real turn loop. What was really called is read
    from the stored messages, not from the reply."""
    from jarvis import assembly, chat_store
    from jarvis.orchestrator.pipeline import ApprovalRequired, Done, Failed, TurnRequest
    from jarvis.policy import Autonomy, Surface

    before = len(chat_store.get_messages(chat))
    reply, failed, cards = "", None, []
    started = time.monotonic()
    for event in assembly.get_orchestrator().run_turn(TurnRequest(
            text=said, session_id=chat, surface=Surface.TEXT, autonomy=Autonomy.INTERACTIVE)):
        if isinstance(event, ApprovalRequired):
            cards.append(event.capability)
        elif isinstance(event, Done):
            reply = event.text or ""
        elif isinstance(event, Failed):
            failed = event.error or event.code or "failed"
    stored = chat_store.get_messages(chat)[before:]
    tools = [call.get("name") for m in stored if m.get("role") == "assistant"
             for call in m.get("toolCalls") or []]
    return {"said": said, "reply": reply, "failed": failed, "cards": cards,
            "tools": [t for t in tools if t], "seconds": round(time.monotonic() - started, 1)}


def _settled() -> bool:
    from jarvis.agents import store as agent_store
    from jarvis.jobs import job_store

    if any(job["status"] in ("queued", "running") for job in job_store.list_active_jobs()):
        return False
    if job_store.list_jobs(status="stalled"):
        return False  # the supervisor has yet to decide what becomes of it
    return not any(run["status"] == "running" for run in agent_store.list_runs(limit=50))


def wait_until_settled(deadline_s: float, poll_s: float = 1.0) -> bool:
    """Until nothing is running or queued any more — twice in a row, so the gap between one
    piece of work ending and the next starting is not mistaken for the end."""
    end = time.monotonic() + deadline_s
    calm = 0
    while time.monotonic() < end:
        calm = calm + 1 if _settled() else 0
        if calm >= 2:
            return True
        time.sleep(poll_s)
    return False


def play(scenario: Scenario, scratch: Path, *, seed: int = 1, phase: str = "start",
         model: Any = None, supervise: bool = True, on_kill_point: Any = None,
         deadline_s: float | None = None) -> dict[str, Any]:
    """Run one phase of a scenario in THIS process, against the data folder the environment
    already points at (`scratch/data`). `phase="start"` builds the world and says the words;
    `phase="resume"` is a fresh process taking over after a kill. `model` replaces the model
    client (a scripted one); otherwise the real one is used. Writes `result-<phase>.json`."""
    from jarvis import assembly, chat_store, conversation
    from jarvis.jobs import orchestrator as supervisor

    started = time.monotonic()
    result: dict[str, Any] = {"scenario": scenario.id, "phase": phase, "seed": seed, "turns": []}
    counts, stop_counting = _count_events()
    _shorten_clocks()
    try:
        world.install()
        orchestrator = assembly.get_orchestrator()
        if model is not None:
            orchestrator._model = model
        fault = scenario.fault
        if fault is not None and fault.kind == "outage" and phase == "start":
            orchestrator._model = Outage(orchestrator._model, fault)
        if fault is not None and fault.kind == "kill" and phase == "start" and on_kill_point:
            from jarvis.events import EventType, bus

            def at_kill_point(event: Any) -> None:
                if counts["backgroundCallsAnswered"] >= fault.after_calls:
                    on_kill_point()

            bus.subscribe(EventType.MODEL_CALL_COMPLETED, at_kill_point)

        if phase == "start":
            world.reset()
            facts = scenario.setup(random.Random(seed))
            (scratch / "facts.json").write_text(json.dumps(facts, indent=1), encoding="utf-8")
        if supervise:
            import os

            os.environ[supervisor.ENABLE_ENV] = "1"
            supervisor.start()          # recovery of anything a dead process left, then its clock
        if phase == "start":
            chat = chat_store.create_conversation()["id"]
            conversation.bind_session(chat)
            result["chat"] = chat
            for said in scenario.say(facts):
                result["turns"].append(_say(chat, said))
        result["settled"] = wait_until_settled(deadline_s or scenario.deadline_s)
    except Exception as err:  # noqa: BLE001 — a broken run is a result, and is reported as one
        result["error"] = f"{type(err).__name__}: {err}"
        result["settled"] = False
    finally:
        stop_counting()
        try:
            supervisor.stop()
        except Exception:  # noqa: BLE001
            pass
        result["events"] = counts
        result["seconds"] = round(time.monotonic() - started, 1)
        (scratch / f"result-{phase}.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    return result


def judge(scenario: Scenario, scratch: Path) -> dict[str, Any]:
    """How a finished run went, read from its folder."""
    records = score.collect(scratch)
    facts = json.loads((scratch / "facts.json").read_text(encoding="utf-8")) \
        if (scratch / "facts.json").exists() else {}
    try:
        met, why = scenario.met(records, facts)
    except Exception as err:  # noqa: BLE001 — a run that left nothing readable did not meet it
        met, why = False, f"could not check the world: {err}"
    outcome = score.outcome_of(records, met)
    violations = records.violations(scenario.allowed_outward)
    checks = records.checks()
    if not met and checks and len(checks) == len(records.jobs) and all(c == "verified" for c in checks):
        # The third hard rule: nothing is called verified that the world says was not done.
        violations.append("delivered as verified, and the world says it was not done")
    problems = []
    if outcome == scenario.expect:
        for check in scenario.extra_checks:
            problem = check(records, facts)
            if problem:
                problems.append(problem)
    errors = [p.get("error") for p in records.harness.get("phases") or [] if p.get("error")]
    passed = outcome == scenario.expect and not violations and not problems and not errors
    calls = records.model_calls()
    return {
        "scenario": scenario.id, "proves": scenario.proves, "expect": scenario.expect,
        "outcome": outcome, "passed": passed, "why": why, "violations": violations,
        "problems": problems + [f"the run itself broke: {e}" for e in errors],
        "modelCalls": calls["made"], "modelCallsAnswered": calls["answered"],
        "questionsAsked": records.questions_asked(),
        "seconds": sum(float(p.get("seconds") or 0) for p in records.harness.get("phases") or []),
        "jobs": [{"status": j["status"], "retries": j["retries"], "error": j.get("error"),
                  "step": j.get("current_step"), "check": records.check_of(j)} for j in records.jobs],
        "toolsCalled": [o["capability"] for o in records.operations],
        "cards": [a["capability"] for a in records.approvals],
        "lastWords": records.last_words()[:600],
        "replies": [(t.get("reply") or t.get("failed") or "")[:400] for t in records.turns],
    }
