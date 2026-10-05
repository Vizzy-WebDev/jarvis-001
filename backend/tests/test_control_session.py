"""The control loop, driven end to end with only the mouse replaced.

The session, the guard, the approval flow and the routes are all the real
implementation. `FakeDesktop` stands in for the hardware and a scripted model
stands in for the decisions, so a whole task — including a risky action a human
has to answer — runs in this container exactly as it would run on a desktop.
"""

from __future__ import annotations

import threading
import time

import pytest
from starlette.testclient import TestClient

from fake_desktop import FakeDesktop, element, window
from jarvis.control import desktop, guard, session as control
from jarvis.control.safety import set_safety_config
from jarvis.orchestrator.model_port import StepComplete, ToolCall


@pytest.fixture(autouse=True)
def _isolated(scratch):
    desktop.reset_for_tests()
    control.reset_for_tests()
    yield
    control.reset_for_tests()
    desktop.reset_for_tests()


@pytest.fixture
def fake():
    fake = FakeDesktop(
        windows_list=[window("1", "Untitled - Notepad", "notepad", foreground=True)],
        elements={"1": [element(0, "Edit", "Text Editor", "", (400, 300)),
                        element(1, "Button", "Save", "", (700, 20))]},
    )
    desktop.set_desktop_for_tests(fake)
    return fake


class ScriptedModel:
    """One scripted decision per step, in order. Runs the real `Gateway.stream`
    contract: a StepComplete carrying tool calls."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.seen: list[str] = []

    def stream(self, *, messages, system, tools, session_id, **kwargs):
        self.seen.append(messages[-1]["text"])
        self.tools = tools
        step = self.steps.pop(0) if self.steps else ToolCall("x", "report_stuck",
                                                             {"reason": "no script left"})
        yield StepComplete(text="", tool_calls=(step,), model_id="scripted")


def call(name, **args):
    return ToolCall(f"c_{name}", name, args)


def run_now(work, name=""):
    """Run the session inline instead of on a thread, so a test asserts on a
    finished session rather than racing one."""
    work()


def never_asked(approval_id, timeout_s):  # pragma: no cover - guard for a bad test
    raise AssertionError("this test should not have needed an approval")


# --- a whole task ------------------------------------------------------------

def test_a_task_runs_to_done_and_says_what_it_did(fake):
    model = ScriptedModel(
        call("perform_actions", reasoning="open the editor",
             actions=[{"kind": "type", "text": "hello there"}]),
        call("report_done", summary="Typed the note."),
    )
    session = control.start("write hello in notepad", plan="type it", client=model,
                            run=run_now, wait=never_asked)

    assert session.status == control.DONE
    assert session.summary == "Typed the note."
    # Focus before typing: a synthetic click does not reliably move keyboard
    # focus, so text can land in the wrong window entirely.
    assert fake.names() == [
        "windows",                       # what was already open, before anything
        "windows", "read_window",        # perceive
        "focus", "type_text",            # act
        "windows", "read_window",        # perceive again — which IS the verification
    ]
    assert fake.typed == ["hello there"]


def test_what_is_on_screen_is_what_the_model_is_shown(fake):
    model = ScriptedModel(call("report_done", summary="nothing to do"))
    control.start("look around", client=model, run=run_now, wait=never_asked)

    shown = model.seen[0]
    assert 'handle 1: "Untitled - Notepad" [notepad] (in front)' in shown
    assert 'id 1: Button "Save"' in shown
    assert "look around" in shown


def test_the_model_is_never_handed_the_chat_tool_catalogue(fake):
    model = ScriptedModel(call("report_done", summary="done"))
    control.start("anything", client=model, run=run_now, wait=never_asked)

    offered = {tool["name"] for tool in model.tools}
    assert offered == {"perform_actions", "report_done", "report_stuck"}
    # And every declaration is only what a model may see — the extra fields a
    # connector carries internally are what made Gemini reject the request.
    for tool in model.tools:
        assert set(tool) == {"name", "description", "parameters"}


def test_a_stuck_session_says_why_rather_than_looking_finished(fake):
    model = ScriptedModel(call("report_stuck", reason="The save dialog never appeared."))
    session = control.start("save it", client=model, run=run_now, wait=never_asked)
    assert session.status == control.STUCK
    assert "save dialog" in session.summary


def test_a_session_that_never_finishes_stops_at_the_step_cap(fake):
    model = ScriptedModel(*[call("perform_actions", reasoning="again",
                                 actions=[{"kind": "scroll", "direction": "down"}])
                            for _ in range(control.MAX_STEPS + 5)])
    session = control.start("scroll forever", client=model, run=run_now, wait=never_asked)
    assert session.status == control.STUCK
    assert session.step == control.MAX_STEPS
    assert "wasn't getting closer" in session.summary


# --- the guard ---------------------------------------------------------------

@pytest.mark.parametrize("action,expected", [
    ({"kind": "scroll"}, guard.SAFE),
    ({"kind": "click", "label": "OK"}, guard.NOTABLE),
    ({"kind": "type", "label": "Buy milk, eggs and bread"}, guard.NOTABLE),
    ({"kind": "type", "label": "rm -rf /"}, guard.RISKY),
    ({"kind": "type", "label": "notes", "description": "delete the file permanently"},
     guard.RISKY),
    ({"kind": "key", "combo": "Enter", "description": "send the email"}, guard.RISKY),
    ({"kind": "COMPOSIO_MULTI_EXECUTE_TOOL"}, guard.RISKY),
    ({"kind": "sharepoint_search", "description": "Search SharePoint."}, guard.NOTABLE),
    ({"kind": "something_new"}, guard.NOTABLE),
])
def test_how_risky_is_that(action, expected):
    """Typed text is scanned for commands, not for everyday words: 'buy' in a
    shopping list is not the same signal as 'buy' in a tool's name."""
    assert guard.classify_action_risk(action) == expected


def test_a_blocked_window_refuses_whatever_the_action_is(fake):
    set_safety_config({"blockedWindowPatterns": ["bank"]})
    verdict = guard.evaluate({"kind": "scroll"}, {"windowTitle": "My Bank - Chrome"},
                             {"blockedWindowPatterns": ["bank"]})
    assert verdict.allowed is False and "blocked pattern" in verdict.reason


def test_acting_in_a_blocked_window_is_refused_mid_session(fake):
    fake.windows_list = [window("9", "Chase Bank — Sign in", "chrome", foreground=True)]
    model = ScriptedModel(
        call("perform_actions", reasoning="log in",
             actions=[{"kind": "type", "text": "hunter2"}]),
        call("report_stuck", reason="I shouldn't be there."),
    )
    session = control.start("log into my bank", client=model, run=run_now, wait=never_asked)
    assert fake.typed == [], "nothing was typed into a blocked window"
    assert session.status == control.STUCK


# --- asking a human ----------------------------------------------------------

def test_a_risky_action_waits_for_a_real_approval_and_then_runs(fake):
    answers = []

    def approve(approval_id, timeout_s):
        from jarvis.policy import approvals as store

        approval = store.get(approval_id)
        answers.append((approval.capability, approval.args["kind"], approval.reason))
        return "allow"

    model = ScriptedModel(
        call("perform_actions", reasoning="clean up",
             actions=[{"kind": "type", "text": "drop table users"}]),
        call("report_done", summary="Done."),
    )
    session = control.start("run that statement", client=model, run=run_now, wait=approve)

    assert answers and answers[0][0] == "control_action"
    assert answers[0][1] == "type"
    assert "Type" in answers[0][2]
    assert fake.typed == ["drop table users"]
    assert session.status == control.DONE


def test_a_declined_action_is_not_taken_and_neither_is_the_rest_of_the_batch(fake):
    model = ScriptedModel(
        call("perform_actions", reasoning="clear it out",
             actions=[{"kind": "type", "text": "drop table users"},
                      {"kind": "key", "combo": "Enter"}]),
        call("report_stuck", reason="They said no."),
    )
    session = control.start("run that statement", client=model, run=run_now,
                            wait=lambda *_: "deny")

    assert fake.typed == []
    assert "key" not in fake.names(), "the rest of the batch never ran either"
    assert session.status == control.STUCK


def test_silence_is_a_decline_never_a_yes(fake):
    model = ScriptedModel(
        call("perform_actions", reasoning="send it",
             actions=[{"kind": "type", "text": "drop table users"}]),
        call("report_stuck", reason="nobody answered"),
    )
    control.start("do the risky thing", client=model, run=run_now,
                  wait=lambda *_: "timeout")
    assert fake.typed == []


def test_the_approval_is_a_real_row_a_restart_would_still_have(fake, scratch):
    """The durable record of consent is the row, not an in-memory promise: the
    original lost the question entirely if the process restarted."""
    seen = {}

    def check(approval_id, timeout_s):
        from jarvis.policy import approvals as store

        seen["pending"] = [a.id for a in store.pending()]
        seen["id"] = approval_id
        return "deny"

    model = ScriptedModel(
        call("perform_actions", reasoning="x",
             actions=[{"kind": "type", "text": "sudo rm -rf /"}]),
        call("report_stuck", reason="no"),
    )
    control.start("dangerous", client=model, run=run_now, wait=check)
    assert seen["id"] in seen["pending"]


def test_an_answer_arrives_through_the_ordinary_approvals_route(fake, scratch):
    """No second confirmation mechanism: the same route the rest of the build
    uses resolves this one, and says it was delivered rather than executed."""
    client = TestClient(__import__("jarvis.main", fromlist=["create_app"]).create_app())
    answered = threading.Event()

    def wait_via_route(approval_id, timeout_s):
        assert control.is_waiting_for(approval_id)
        body = client.post(f"/api/approvals/{approval_id}", json={"decision": "allow"}).json()
        assert body["ran"] is False and body["delivered"] is True
        answered.set()
        return control._wait_for_approval(approval_id, 5)

    model = ScriptedModel(
        call("perform_actions", reasoning="x",
             actions=[{"kind": "type", "text": "drop table users"}]),
        call("report_done", summary="Done."),
    )
    session = control.start("do it", client=model, run=run_now, wait=wait_via_route)
    assert answered.is_set()
    assert fake.typed == ["drop table users"]
    assert session.status == control.DONE


# --- closing windows ---------------------------------------------------------

def test_closing_a_window_the_user_had_open_asks_first(fake):
    asked = []
    model = ScriptedModel(
        call("perform_actions", reasoning="tidy", actions=[{"kind": "close_window",
                                                            "windowHandle": "1"}]),
        call("report_done", summary="Closed it."),
    )
    control.start("close notepad", client=model, run=run_now,
                  wait=lambda approval_id, t: (asked.append(approval_id), "deny")[1])
    assert asked, "a window the user already had open is theirs to decide about"
    assert fake.closed == []


def test_closing_scratch_jarvis_opened_itself_does_not(fake):
    model = ScriptedModel(
        call("perform_actions", reasoning="open one",
             actions=[{"kind": "launch_app", "app": "notepad"}]),
        call("perform_actions", reasoning="close it",
             actions=[{"kind": "close_window", "windowHandle": "77"}]),
        call("report_done", summary="Done."),
    )

    def launching(**kwargs):
        # The app really appears, the way a launch is polled for rather than
        # slept on: a window that is not there yet reads as a failed launch.
        fake.windows_list.append(window("77", "Untitled - Notepad", "notepad"))

    fake.launch = lambda app: (fake._record("launch", app), launching())[0]
    control.start("open and close a scratch window", client=model, run=run_now,
                  wait=never_asked)
    assert fake.closed == ["77"]


def test_what_jarvis_opened_is_tidied_up_and_the_users_own_window_is_not(fake):
    model = ScriptedModel(
        call("perform_actions", reasoning="open one",
             actions=[{"kind": "launch_app", "app": "notepad"}]),
        call("report_done", summary="Done."),
    )

    def launching(app):
        fake._record("launch", app)
        fake.windows_list.append(window("88", "Scratch - Notepad", "notepad"))

    fake.launch = launching
    control.start("open something", client=model, run=run_now, wait=never_asked)
    assert fake.closed == ["88"], "only the window this session opened"


# --- stopping ----------------------------------------------------------------

def test_moving_your_own_mouse_stops_the_session(fake):
    steps = [call("perform_actions", reasoning="click it",
                  actions=[{"kind": "click", "elementId": 1}]),
             call("perform_actions", reasoning="click again",
                  actions=[{"kind": "click", "elementId": 1}]),
             call("report_done", summary="never reached")]
    model = ScriptedModel(*steps)

    real_cursor = fake.cursor

    def drifting():
        position = real_cursor()
        # A hand on the mouse between the first and second click.
        return (position[0] + 500, position[1]) if fake.names().count("click") else position

    fake.cursor = drifting
    session = control.start("click twice", client=model, run=run_now, wait=never_asked)
    assert session.status == control.STOPPED
    assert "moved the mouse" in session.summary
    assert fake.names().count("click") == 1


def test_asking_it_to_stop_ends_the_session_rather_than_pausing_it(fake):
    model = ScriptedModel(
        call("perform_actions", reasoning="one", actions=[{"kind": "scroll"}]),
        call("perform_actions", reasoning="two", actions=[{"kind": "scroll"}]),
    )
    original = fake.scroll

    def stop_after_first(amount):
        original(amount)
        control.request_stop("you asked me to stop")

    fake.scroll = stop_after_first
    session = control.start("scroll", client=model, run=run_now, wait=never_asked)
    assert session.status == control.STOPPED
    assert fake.names().count("scroll") == 1


def test_only_one_session_at_a_time(fake):
    slow = threading.Event()

    class Blocking(ScriptedModel):
        def stream(self, **kwargs):
            slow.wait(2)
            yield StepComplete(text="", tool_calls=(call("report_done", summary="ok"),))

    control.start("first", client=Blocking(), wait=never_asked)
    time.sleep(0.05)
    with pytest.raises(RuntimeError, match="already working"):
        control.start("second", client=ScriptedModel(), wait=never_asked)
    slow.set()


# --- the routes --------------------------------------------------------------

def test_status_and_stop_over_http(fake):
    client = TestClient(__import__("jarvis.main", fromlist=["create_app"]).create_app())
    # Byte-identical to what the recorded API answers when nothing is running:
    # every existing caller was written against exactly these four fields.
    assert client.get("/api/control/status").json() == {
        "active": False, "step": "", "awaitingConfirmation": False, "pendingSummary": None}
    assert client.post("/api/control/stop").json()["ok"] is False

    holding = threading.Event()

    class Blocking(ScriptedModel):
        def stream(self, **kwargs):
            holding.wait(2)
            yield StepComplete(text="", tool_calls=(call("report_stuck", reason="stopped"),))

    control.start("something long", client=Blocking(), wait=never_asked)
    time.sleep(0.05)
    body = client.get("/api/control/status").json()
    assert body["active"] is True and body["goal"] == "something long"
    assert client.post("/api/control/stop", json={"reason": "the button"}).json()["ok"] is True
    holding.set()


def test_the_tool_plans_first_and_starts_only_when_confirmed(fake, monkeypatch):
    from jarvis.tools import control_computer

    monkeypatch.setattr(control_computer.control, "prepare_plan",
                        lambda goal, **kw: "Open Notepad. Type it. Save.")
    first = control_computer.SPEC.handler(goal="write a note")
    assert first["stage"] == "plan" and "Notepad" in first["plan"]
    # Nothing has started: a plan is a description, not a beginning.
    assert control.status()["active"] is False

    started = {}
    monkeypatch.setattr(control_computer.control, "start",
                        lambda goal, plan="", **kw: started.setdefault(
                            "call", type("S", (), {"id": "ctl_x", "goal": goal,
                                                    "plan": plan})()))
    second = control_computer.SPEC.handler(goal="write a note", confirmed=True)
    assert second["stage"] == "started"
    # The plan the user agreed to is the one the session gets.
    assert started["call"].plan == "Open Notepad. Type it. Save."


def test_control_is_the_highest_risk_so_no_schedule_can_pre_consent_it():
    from jarvis.capabilities import Risk
    from jarvis.tools.control_computer import SPEC

    assert SPEC.risk is Risk.HIGH


# --- the two stops that live outside the loop --------------------------------

def test_the_hotkey_watcher_stops_the_session_when_the_combination_is_held():
    """Polled rather than registered: a registered hotkey can be refused when
    something else already owns the combination, and it fails by never firing —
    the worst failure mode for the control someone reaches for in a hurry."""
    from jarvis.control.overlay import watch_for_hotkey

    stopped = threading.Event()
    running = threading.Event()
    running.set()
    held = threading.Event()

    watch_for_hotkey(lambda: (stopped.set(), running.clear()),
                     running.is_set, poll_seconds=0.01, pressed=held.is_set)
    time.sleep(0.05)
    assert not stopped.is_set(), "nothing pressed, nothing stopped"
    held.set()
    assert stopped.wait(2), "holding the combination stops the session"


def test_the_watcher_gives_up_when_the_session_ends():
    from jarvis.control.overlay import watch_for_hotkey

    running = threading.Event()
    running.set()
    thread = watch_for_hotkey(lambda: None, running.is_set, poll_seconds=0.01,
                              pressed=lambda: False)
    running.clear()
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_a_session_still_runs_when_no_overlay_can_be_drawn(fake, monkeypatch):
    """There is no display here, so the window genuinely cannot open — and the
    session must continue, because the overlay is one of three stops, not the
    only one."""
    from jarvis.control import overlay

    shown = {}
    monkeypatch.setattr(overlay.Overlay, "show",
                        lambda self, goal, url: shown.setdefault("tried", True) and False)
    monkeypatch.setattr(overlay.Overlay, "hide", lambda self: None)

    model = ScriptedModel(call("report_done", summary="Fine without it."))
    session = control.start("do something", client=model, wait=never_asked)
    for _ in range(100):
        if session.finished_at:
            break
        time.sleep(0.02)
    assert shown.get("tried") is True
    assert session.status == control.DONE


def test_the_overlay_child_never_inherits_the_users_keys(monkeypatch):
    """It draws a window. It has no business holding an API key."""
    from jarvis.control.overlay import Overlay

    monkeypatch.setenv("GEMINI_API_KEY", "AIzaNOTFORTHEOVERLAY")
    monkeypatch.setenv("JARVIS_SECRET_SOMETHING", "nope")
    seen = {}

    class FakePopen:
        def __init__(self, args, env=None, **kwargs):
            seen["env"] = env

        def poll(self):
            return None

    monkeypatch.setattr("subprocess.Popen", FakePopen)
    assert Overlay().show("a goal", "http://127.0.0.1:1/api/control/stop") is True
    assert "GEMINI_API_KEY" not in seen["env"]
    assert "JARVIS_SECRET_SOMETHING" not in seen["env"]


def test_the_self_check_refuses_where_there_is_no_desktop(capsys):
    """It is the thing that proves the primitives on a real machine, so it must
    never print a pass on a machine that has no screen to prove them against."""
    from jarvis.control import selfcheck

    assert selfcheck.main() == 1
    printed = capsys.readouterr().out
    assert "no desktop to check here" in printed
    assert "PASS" not in printed
