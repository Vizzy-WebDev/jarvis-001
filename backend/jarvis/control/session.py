"""Driving the computer toward a goal: plan, look, decide, check, act, look again.

Its own loop rather than the chat turn loop, for the same reason the original
kept them apart: a control session is long-running, carries a fresh view of the
screen every step, and must never be able to reach the chat tool catalogue —
rescheduling itself mid-click is not something a desktop task should be able to
do. Its actions are the fixed set below, plus whatever connectors the user has
already enabled.

Three things worth knowing before changing anything here:

* **Looking again IS the verification.** There is no separate "did that work"
  step: the next perceive shows the result of the last action, and the model
  decides whether to continue, retry or finish. That is why the loop is shaped
  this way and why a batch of actions is small.
* **A risky action goes through the ordinary approvals seam** — the same
  persisted row, the same `POST /api/approvals/{id}`, the same same-turn
  refusal — rather than an in-memory promise of its own. The original had a
  bespoke mechanism here, which meant a missed event hung the session forever
  until a timeout was bolted on, and a restart lost the question entirely.
* **Connector declarations are stripped before they reach a model.** They carry
  bookkeeping fields internally, and sending those made Gemini reject the whole
  request — every DECIDE call, any time a connector was enabled, which is
  effectively always. Two shapes for two audiences, exactly as the chat path
  already does it.

Three independent stops, all of which end the session rather than pausing it:
the overlay's button, the global hotkey, and moving your own mouse.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from ..events import EventType, bus as default_bus
from . import guard
from .overlay import HOTKEY_LABEL
from .desktop import NoDesktopHere, Window, get_desktop, is_jarvis_own_window
from .safety import get_safety_config

logger = logging.getLogger(__name__)

#: Past this, a session is not making progress; it is looping.
MAX_STEPS = 25
#: Small batches on purpose — the screen after each one is the only real
#: evidence that the last one worked.
MAX_ACTIONS_PER_BATCH = 4
#: Generous: a free-tier or reasoning model can take a while to answer.
DECIDE_TIMEOUT_S = 45.0
#: How long a risky action waits for a human before it gives up. A timeout
#: resolves as a decline — the same outcome as a real "no", never as a yes.
CONFIRM_TIMEOUT_S = 10 * 60.0
#: A real hand on the mouse moves further than this between two actions.
CURSOR_DRIFT_TOLERANCE = 4
#: How many observations of the screen to keep in front of the model.
HISTORY_STEPS = 6

SYSTEM_INSTRUCTION = """You are Jarvis's computer-control agent. You operate the user's Windows computer directly — moving the mouse, clicking, typing, and reading what is on screen — to accomplish a stated goal.

Each turn you are shown every open window, the front one in more detail, and either a list of its on-screen elements (id, role, name, value) or a note that only a picture was available. Element ids come from the list you were shown THIS turn; they are not stable between turns.

About the user's other windows:
- If the app you need is already open, switch to it rather than launching a second copy.
- Minimising or switching away is how you move on from a window you are merely done with for now. Treat anything that was already open as the user's own workspace: leave it as you found it unless the goal says otherwise.
- You can close a window when the goal calls for it, or when it is scratch you opened yourself. Closing anything the user already had open pauses to ask them — that happens automatically, you do not need to ask in words as well. Closing only ever requests it, like clicking the window's X; the app may put up an unsaved-changes prompt, which is normal, not a failure.
- A task can span several windows. Switch between them as often as you need.
- When you are finished, anything Jarvis itself opened as scratch is tidied up for you. Just call report_done.

Respond by calling exactly one tool:
- perform_actions: one or a few concrete actions to take right now. Keep batches small and look at the result before assuming a long sequence worked.
- report_done: the goal is genuinely accomplished. Say briefly what you did.
- report_stuck: you cannot proceed — wrong app, missing element, an unexpected dialog. Explain plainly.
- any connected-service tool offered to you this turn, when the goal calls for it.

Never invent an element id or a window handle that was not in the list you were just shown. If you are unsure what is on screen, take one small action and look again rather than guessing a long sequence."""

_ACTION_KINDS = ["launch_app", "switch_window", "minimize_window", "restore_window",
                 "arrange_window", "close_window", "click", "double_click",
                 "right_click", "type", "key", "scroll", "wait"]

CONTROL_TOOLS: list[dict[str, Any]] = [
    {
        "name": "perform_actions",
        "description": "Perform one or a few concrete actions on the computer right now.",
        "parameters": {
            "type": "object",
            "properties": {
                "reasoning": {"type": "string",
                              "description": "One short sentence on why these actions move "
                                             "toward the goal."},
                "actions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string", "enum": _ACTION_KINDS},
                            "app": {"type": "string",
                                    "description": 'For "launch_app" — a friendly app name.'},
                            "windowHandle": {"type": "string",
                                             "description": "For the window actions — a handle "
                                                            "from the current window list."},
                            "elementId": {"type": "number",
                                          "description": "For a click — the id from the element "
                                                         "list you were just shown."},
                            "x": {"type": "number"}, "y": {"type": "number"},
                            "width": {"type": "number"}, "height": {"type": "number"},
                            "text": {"type": "string", "description": 'For "type".'},
                            "combo": {"type": "string",
                                      "description": 'For "key" — e.g. "Enter", "Ctrl+S".'},
                            "direction": {"type": "string", "enum": ["up", "down"]},
                            "amount": {"type": "number",
                                       "description": 'For "scroll" — notches, default 3.'},
                            "seconds": {"type": "number",
                                        "description": 'For "wait" — at most 5.'},
                        },
                        "required": ["kind"],
                    },
                },
            },
            "required": ["actions"],
        },
    },
    {
        "name": "report_done",
        "description": "Call once the goal has genuinely been accomplished.",
        "parameters": {"type": "object", "properties": {"summary": {"type": "string"}},
                       "required": ["summary"]},
    },
    {
        "name": "report_stuck",
        "description": "Call if you cannot proceed toward the goal.",
        "parameters": {"type": "object", "properties": {"reason": {"type": "string"}},
                       "required": ["reason"]},
    },
]

PLANNING = "planning"
RUNNING = "running"
AWAITING_CONFIRMATION = "awaiting_confirmation"
DONE = "done"
STUCK = "stuck"
STOPPED = "stopped"
FAILED = "failed"


@dataclass
class ControlSession:
    id: str
    goal: str
    plan: str = ""
    status: str = PLANNING
    step: int = 0
    summary: str = ""
    error: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    #: Windows this session opened, so tidying up can never touch the user's own.
    created_handles: set[str] = field(default_factory=set)
    pre_existing_handles: set[str] = field(default_factory=set)
    actions_taken: list[str] = field(default_factory=list)
    approval_id: str | None = None
    stop_reason: str = ""
    _stop: threading.Event = field(default_factory=threading.Event)

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "goal": self.goal, "plan": self.plan,
                "status": self.status, "step": self.step, "summary": self.summary,
                "error": self.error, "actions": list(self.actions_taken),
                "approvalId": self.approval_id,
                "stopReason": self.stop_reason,
                "startedAt": self.started_at, "finishedAt": self.finished_at,
                "active": self.status in (PLANNING, RUNNING, AWAITING_CONFIRMATION)}


_lock = threading.RLock()
_active: ControlSession | None = None


def active() -> ControlSession | None:
    with _lock:
        return _active


def status() -> dict[str, Any]:
    session = active()
    return session.as_dict() if session else {"active": False, "status": "idle"}


def request_stop(reason: str = "you asked me to stop") -> dict[str, Any]:
    """Any of the three stops. Ends the session — it does not pause it: a person
    reaching for their own mouse means "that's enough", not "hold on"."""
    session = active()
    if session is None:
        return {"ok": False, "error": "Nothing is running."}
    session.stop_reason = reason
    session._stop.set()
    return {"ok": True, "stopping": session.id, "reason": reason}


def is_waiting_for(approval_id: str) -> bool:
    """Whether a control session is sitting on this approval.

    Read by the approvals route: such an approval is answered by something
    already running, not executed on demand from the registry.
    """
    session = active()
    return bool(session and session.approval_id and session.approval_id == approval_id)


# --- looking -----------------------------------------------------------------

def _front_window(windows: list[Window]) -> Window | None:
    """The window to act on — never Jarvis's own tab, for the same reason
    `look_at_screen` excludes it: the user is usually talking to Jarvis when a
    session starts, so the foreground window is very often Jarvis itself."""
    others = [w for w in windows if not is_jarvis_own_window(w)]
    pool = others or windows
    return next((w for w in pool if w.foreground), None) or (pool[0] if pool else None)


def _describe(desktop: Any, windows: list[Window], front: Window | None) -> tuple[str, list]:
    lines = ["Open windows:"]
    for window in windows:
        mark = " (in front)" if window.foreground else ""
        lines.append(f'  handle {window.handle}: "{window.title}" [{window.process_name}]{mark}')
    elements: list = []
    if front is not None:
        lines.append(f'\nThe window being worked in is "{front.title}" ({front.process_name}).')
        try:
            elements = desktop.read_window(front.handle)
        except Exception as err:  # noqa: BLE001
            logger.debug("could not read %s: %s", front.handle, err)
            elements = []
        if elements:
            lines.append("Its elements:")
            for element in elements:
                piece = f"  id {element.id}: {element.role}"
                if element.name:
                    piece += f' "{element.name}"'
                if element.value:
                    piece += f' = "{element.value[:120]}"'
                lines.append(piece)
        else:
            lines.append("Its contents could not be read as a list this time — act from the "
                         "window title and what you already know, or take one small step and "
                         "look again.")
    return "\n".join(lines), elements


# --- acting ------------------------------------------------------------------

def _label_of(action: dict[str, Any]) -> str:
    """What the guard reads as this action's own text. For a `type` action it is
    the literal text being typed, which is why the guard scans it differently."""
    kind = str(action.get("kind") or "")
    if kind == "type":
        return str(action.get("text") or "")
    return str(action.get("app") or action.get("combo") or action.get("windowHandle") or "")


def _element_by_id(elements: list, wanted: Any) -> Any:
    try:
        target = int(wanted)
    except (TypeError, ValueError):
        return None
    return next((e for e in elements if e.id == target), None)


class _Aborted(RuntimeError):
    """A stop, a refusal or a decline. Ends the batch, not just the action."""


def _perform(session: ControlSession, desktop: Any, action: dict[str, Any],
             front: Window | None, elements: list) -> str:
    """One action. Returns a short line saying what happened, for the next
    observation — the model never gets a bare 'ok'."""
    kind = str(action.get("kind") or "")

    if kind == "wait":
        seconds = min(float(action.get("seconds") or 1), 5.0)
        time.sleep(seconds)
        return f"waited {seconds:g}s"

    if kind == "launch_app":
        app = str(action.get("app") or "")
        before = {w.handle for w in desktop.windows()}
        desktop.launch(app)
        # Poll rather than sleeping a fixed time: a cold-starting app can miss a
        # fixed wait entirely, and a window that is not there yet reads to the
        # model as a failed launch — which is how a second copy gets opened.
        appeared: set[str] = set()
        deadline = time.time() + 5
        while time.time() < deadline:
            time.sleep(0.3)
            appeared = {w.handle for w in desktop.windows()} - before
            if appeared:
                break
        session.created_handles |= appeared
        return (f'launched "{app}"' if appeared
                else f'launched "{app}" but no new window appeared yet')

    handle = str(action.get("windowHandle") or (front.handle if front else ""))
    if kind == "switch_window":
        desktop.focus(handle)
        return f"switched to window {handle}"
    if kind == "minimize_window":
        desktop.minimize(handle)
        return f"minimised window {handle}"
    if kind == "restore_window":
        desktop.restore(handle)
        return f"restored window {handle}"
    if kind == "arrange_window":
        desktop.arrange(handle, int(action.get("x") or 0), int(action.get("y") or 0),
                        int(action.get("width") or 800), int(action.get("height") or 600))
        return f"moved window {handle}"
    if kind == "close_window":
        desktop.close(handle)
        session.created_handles.discard(handle)
        return f"asked window {handle} to close"

    if kind in ("click", "double_click", "right_click"):
        element = _element_by_id(elements, action.get("elementId"))
        if element is None or not element.center:
            return (f"could not click element {action.get('elementId')} — it is not in the "
                    "list you were shown")
        _check_drift(session, desktop)
        if front is not None:
            desktop.focus(front.handle)
        button = "right" if kind == "right_click" else "left"
        desktop.click(element.center[0], element.center[1], button=button,
                      double=kind == "double_click")
        return f'{kind.replace("_", " ")}ed "{element.name or element.role}"'

    if kind == "type":
        # Focus first, always: a synthetic click does not reliably move keyboard
        # focus, so text can otherwise land in a completely different window.
        if front is not None:
            desktop.focus(front.handle)
        text = str(action.get("text") or "")
        desktop.type_text(text)
        return f"typed {len(text)} characters"

    if kind == "key":
        if front is not None:
            desktop.focus(front.handle)
        combo = str(action.get("combo") or "")
        desktop.key(combo)
        return f"pressed {combo}"

    if kind == "scroll":
        _check_drift(session, desktop)
        amount = int(action.get("amount") or 3)
        direction = str(action.get("direction") or "down")
        desktop.scroll(-amount if direction == "down" else amount)
        return f"scrolled {direction}"

    return f'I do not know how to "{kind}"'


def _check_drift(session: ControlSession, desktop: Any) -> None:
    """Did the user take the mouse back? Compared before every synthetic mouse
    action, against where WE last left the cursor."""
    expected = getattr(session, "_cursor", None)
    try:
        actual = desktop.cursor()
    except Exception:  # noqa: BLE001 — no cursor reading is not a reason to stop
        return
    if expected is not None:
        drift = max(abs(actual[0] - expected[0]), abs(actual[1] - expected[1]))
        if drift > CURSOR_DRIFT_TOLERANCE:
            session.stop_reason = "you moved the mouse, so I stopped"
            session._stop.set()
            raise _Aborted(session.stop_reason)
    session._cursor = actual  # type: ignore[attr-defined]


# --- asking ------------------------------------------------------------------

def _ask_permission(session: ControlSession, action: dict[str, Any], summary: str,
                    *, event_bus: Any = None, wait: Callable[[str, float], str] | None = None
                    ) -> bool:
    """Raise a real approval and wait for a real answer.

    The row is the durable record of consent, so a restart does not lose the
    question — and the answer arrives through the same route every other
    approval in this build uses.
    """
    from ..capabilities import CapabilitySpec, Risk
    from ..policy import Autonomy, CallContext, Surface
    from ..policy import approvals as approvals_store

    spec = CapabilitySpec(
        id="control.action", name="control_action",
        description="An action Jarvis wants to take on your computer.",
        input_schema={"type": "object", "properties": {}},
        risk=Risk.HIGH, handler=lambda **_: None,
    )
    ctx = CallContext(
        session_id=f"control:{session.id}",
        # A real, distinct turn id per question: the same-turn refusal is what
        # stops a question being asked and answered without a person in between,
        # and a session that reused one id would be exempting itself from it.
        turn_id=f"control-step-{session.step}-{uuid.uuid4().hex[:6]}",
        surface=Surface.TEXT,
        autonomy=Autonomy.INTERACTIVE,
        operation_id=f"control-{session.id}-{session.step}-{uuid.uuid4().hex[:6]}",
    )
    approval = approvals_store.request(spec, dict(action), ctx, summary, event_bus=event_bus)
    session.approval_id = approval.id
    session.status = AWAITING_CONFIRMATION
    try:
        outcome = (wait or _wait_for_approval)(approval.id, CONFIRM_TIMEOUT_S)
    finally:
        session.approval_id = None
        if session.status == AWAITING_CONFIRMATION:
            session.status = RUNNING
    return outcome == "allow"


def _wait_for_approval(approval_id: str, timeout_s: float) -> str:
    """Poll the store until it is answered, or time out.

    Polling rather than subscribing: the answer is a database row either way,
    and a session that missed a single in-memory event would wait forever — the
    exact failure the original had to add a timeout to paper over.
    """
    from ..policy import approvals as approvals_store

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        approval = approvals_store.get(approval_id)
        if approval is None:
            return "gone"
        if not approval.is_pending:
            return approval.status.value
        session = active()
        if session is not None and session.stopped:
            return "stopped"
        time.sleep(0.5)
    # Nobody answered. A timeout is a decline: the safe reading of silence is
    # "no", never "go ahead".
    return "timeout"


# --- the loop ----------------------------------------------------------------

def _decide(client: Any, session: ControlSession, observation: str, history: list[str],
            tools: list[dict[str, Any]]) -> Any:
    from ..orchestrator.model_port import StepComplete

    recent = "\n".join(history[-HISTORY_STEPS:])
    message = (f"The goal: {session.goal}\n\n"
               + (f"The plan you agreed:\n{session.plan}\n\n" if session.plan else "")
               + (f"What has happened so far:\n{recent}\n\n" if recent else "")
               + f"On screen right now:\n{observation}\n\n"
               "Decide the next step and call exactly one tool.")
    step: Any = None
    for event in client.stream(messages=[{"role": "user", "text": message}],
                               system=SYSTEM_INSTRUCTION, tools=tools,
                               session_id=f"control:{session.id}", background=True):
        if isinstance(event, StepComplete):
            step = event
    return step


def _connector_tools() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """`(declarations, specs_by_name)` for the connectors the user has enabled.

    Declarations are stripped to what a model may see. Sending anything else is
    what made Gemini reject the entire request — every decide call, any time a
    connector was enabled.
    """
    try:
        from ..connectors import capabilities as connector_capabilities
        from ..connectors import store as connector_store
    except Exception:  # noqa: BLE001
        return [], {}
    declarations: list[dict[str, Any]] = []
    by_name: dict[str, Any] = {}
    try:
        connectors = [c for c in connector_store.list_connectors() if c.get("enabled", True)]
    except Exception:  # noqa: BLE001
        return [], {}
    for connector in connectors:
        try:
            for spec in connector_capabilities.connector_specs(connector):
                by_name[spec.name] = spec
                declarations.append({"name": spec.name, "description": spec.description,
                                     "parameters": spec.input_schema})
        except Exception as err:  # noqa: BLE001 — one broken connector is not the session's problem
            logger.info("skipping connector %s: %s", connector.get("id"), err)
    return declarations, by_name


def _run_session(session: ControlSession, client: Any, event_bus: Any,
                 wait: Callable[[str, float], str] | None,
                 show_overlay: bool = True) -> None:
    bus = event_bus or default_bus
    desktop = get_desktop()
    config = get_safety_config()
    connector_declarations, connector_specs = _connector_tools()
    tools = CONTROL_TOOLS + connector_declarations
    history: list[str] = []

    def announce() -> None:
        bus.publish(EventType.CONTROL_SESSION, session.as_dict())

    session.status = RUNNING
    announce()

    # Two of the three stops start here; the third (a hand on the mouse) is
    # checked inside the loop, where the cursor is.
    bar = None
    if show_overlay:
        from .overlay import Overlay, stop_url, watch_for_hotkey

        bar = Overlay()
        bar.show(session.goal, stop_url())
        watch_for_hotkey(lambda: request_stop(f"you pressed {HOTKEY_LABEL}"),
                         lambda: not session.stopped and session.finished_at is None)

    try:
        session.pre_existing_handles = {w.handle for w in desktop.windows()}
    except NoDesktopHere as err:
        session.status = FAILED
        session.error = str(err)
        session.finished_at = time.time()
        if bar is not None:
            bar.hide()
        announce()
        return

    try:
        for step in range(1, MAX_STEPS + 1):
            if session.stopped:
                session.status = STOPPED
                session.summary = session.stop_reason
                break
            session.step = step

            windows = desktop.windows()
            front = _front_window(windows)
            observation, elements = _describe(desktop, windows, front)

            decision = _decide(client, session, observation, history, tools)
            if decision is None or not decision.tool_calls:
                history.append(f"step {step}: no action was decided on")
                continue

            call = decision.tool_calls[0]
            if call.name == "report_done":
                session.status = DONE
                session.summary = str(call.args.get("summary") or "Finished.")
                break
            if call.name == "report_stuck":
                session.status = STUCK
                session.summary = str(call.args.get("reason") or "I couldn't get any further.")
                break

            if call.name in connector_specs:
                outcome = _run_connector(session, connector_specs[call.name], call, config)
                history.append(f"step {step}: {outcome}")
                announce()
                continue

            if call.name != "perform_actions":
                history.append(f"step {step}: unknown tool {call.name}")
                continue

            actions = list(call.args.get("actions") or [])[:MAX_ACTIONS_PER_BATCH]
            reasoning = str(call.args.get("reasoning") or "")
            try:
                results = _act(session, desktop, actions, reasoning, front, elements,
                               config, event_bus=bus, wait=wait)
            except _Aborted as stop:
                history.append(f"step {step}: {stop}")
                session.status = STOPPED
                session.summary = str(stop)
                break
            history.append(f"step {step}: " + "; ".join(results))
            session.actions_taken.extend(results)
            announce()
        else:
            session.status = STUCK
            session.summary = (f"I stopped after {MAX_STEPS} steps without finishing — "
                               "it wasn't getting closer.")
    except NoDesktopHere as err:
        session.status = FAILED
        session.error = str(err)
    except Exception as err:  # noqa: BLE001 — a session must end with a status, always
        logger.exception("control session %s failed", session.id)
        session.status = FAILED
        session.error = str(err)

    _tidy_up(session, desktop)
    session.finished_at = time.time()
    if bar is not None:
        bar.hide()
    announce()


def _act(session: ControlSession, desktop: Any, actions: list[dict[str, Any]],
         reasoning: str, front: Window | None, elements: list, config: dict[str, Any],
         *, event_bus: Any, wait: Callable[[str, float], str] | None) -> list[str]:
    """One batch. Every action gets a result line, including the ones that never
    ran — an action with no entry at all reads as "it worked too"."""
    results: list[str] = []
    remaining_blocked = ""
    for action in actions:
        if remaining_blocked:
            results.append(f'"{action.get("kind")}" was not attempted ({remaining_blocked})')
            continue
        if session.stopped:
            raise _Aborted(session.stop_reason or "stopped")

        subject = {"windowTitle": front.title if front else "",
                   "processName": front.process_name if front else ""}
        judged = guard.evaluate({**action, "label": _label_of(action),
                                 "description": reasoning}, subject, config)
        if not judged.allowed:
            results.append(f'"{action.get("kind")}" was refused — {judged.reason}')
            remaining_blocked = "an earlier action in this batch was refused"
            continue

        needs_confirm = judged.needs_confirm
        if action.get("kind") == "close_window":
            # Closing something the user already had open is theirs to decide,
            # however safe closing looks in the abstract.
            handle = str(action.get("windowHandle") or (front.handle if front else ""))
            if handle not in session.created_handles:
                needs_confirm = True

        if needs_confirm:
            summary = _confirm_text(action, front)
            if not _ask_permission(session, action, summary, event_bus=event_bus, wait=wait):
                results.append(f'"{action.get("kind")}" was not approved, so I left it alone')
                remaining_blocked = "the action before it was declined"
                continue

        try:
            results.append(_perform(session, desktop, action, front, elements))
        except _Aborted:
            raise
        except Exception as err:  # noqa: BLE001 — one failed action is information, not a crash
            results.append(f'"{action.get("kind")}" failed: {err}')
            remaining_blocked = "the action before it failed"
    return results


def _confirm_text(action: dict[str, Any], front: Window | None) -> str:
    kind = str(action.get("kind") or "do that")
    where = f' in "{front.title}"' if front else ""
    if kind == "type":
        return f'Type "{str(action.get("text") or "")[:120]}"{where}?'
    if kind == "close_window":
        return f"Close{where or ' that window'}?"
    if kind == "key":
        return f'Press {action.get("combo")}{where}?'
    return f"{kind.replace('_', ' ').capitalize()}{where}?"


def _run_connector(session: ControlSession, spec: Any, call: Any,
                   config: dict[str, Any]) -> str:
    """A connected service's tool, called mid-task. Same guard, same gate."""
    from ..capabilities import Risk

    if spec.risk is not Risk.LOW:
        allowed = _ask_permission(session, {"kind": call.name, "args": dict(call.args)},
                                  f"Use {call.name}?")
        if not allowed:
            return f"{call.name} was not approved"
    try:
        result = spec.handler(**dict(call.args))
    except Exception as err:  # noqa: BLE001
        return f"{call.name} failed: {err}"
    return f"{call.name} ran: {str(result)[:200]}"


def _tidy_up(session: ControlSession, desktop: Any) -> None:
    """Close what Jarvis opened, and nothing else.

    Only handles this session created and still has: anything the user already
    had open is theirs, and anything the model deliberately kept is not scratch.
    """
    if session.status not in (DONE, STUCK):
        return
    for handle in list(session.created_handles):
        if handle in session.pre_existing_handles:
            continue
        try:
            desktop.close(handle)
        except Exception as err:  # noqa: BLE001
            logger.debug("could not tidy up window %s: %s", handle, err)


# --- starting ----------------------------------------------------------------

def prepare_plan(goal: str, *, client: Any = None) -> str:
    """One model call: what it intends to do, in plain language, before anything
    happens. This is what the user actually approves."""
    from ..ai import ask_model

    reply = ask_model(
        "Someone has asked for this to be done on their Windows computer:\n\n"
        f"{goal}\n\n"
        "Describe in at most four short steps how you would do it by clicking and typing, "
        "in plain language, as you would explain it to the person watching. No numbered "
        "preamble, no promises, just the steps.",
        system="You plan short desktop tasks. Be concrete and brief.")
    return reply.text.strip() if reply.ok else ""


def start(goal: str, plan: str = "", *, client: Any = None, event_bus: Any = None,
          wait: Callable[[str, float], str] | None = None,
          run: Callable[[Callable[[], None], str], Any] | None = None) -> ControlSession:
    """Begin a session. One at a time, deliberately: two things driving the same
    mouse is not a feature."""
    global _active
    with _lock:
        current = _active
        if current is not None and current.as_dict()["active"]:
            raise RuntimeError("I'm already working on something on your computer.")
        session = ControlSession(id=f"ctl_{uuid.uuid4().hex[:10]}", goal=goal, plan=plan)
        _active = session

    if client is None:
        from ..gateway.client import Gateway

        client = Gateway()

    def work() -> None:
        # A test drives the loop with its own runner and has no screen to draw
        # on; the overlay belongs to a session that is really taking over.
        _run_session(session, client, event_bus, wait, show_overlay=run is None)

    if run is not None:
        run(work, f"control:{session.id}")
    else:
        from ..background import run_in_background

        run_in_background(work, name=f"control:{session.id}")
    return session


def reset_for_tests() -> None:
    global _active
    with _lock:
        if _active is not None:
            _active._stop.set()
        _active = None
