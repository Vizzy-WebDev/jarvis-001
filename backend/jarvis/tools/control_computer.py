"""Hand the computer over: Jarvis clicks and types toward a goal.

**Two calls, always.** The first returns a plan and takes over nothing; the
user reads it and says yes; the second, carrying `confirmed`, actually starts.
That is not a formality — taking over someone's mouse and keyboard is the most
consequential thing this build does, and the plan is the thing they are agreeing
to. The risk level is HIGH, so it is never pre-consented by a scheduled task or
a briefing either: a background trigger cannot start a control session.

The spoken "I'm taking over now" heads-up is a prompt rule for the confirmed
call rather than anything here — it is ordinary reply text, in Jarvis's own
words, spoken like any other reply.
"""

from __future__ import annotations

from ..capabilities import CapabilitySpec, Risk
from ..control import available, describe_desktop
from ..control import session as control


#: The plan most recently produced, so the confirmed call starts the session the
#: user actually agreed to rather than a fresh one the model reworded. Keyed by
#: goal: a second, different request never inherits the first one's plan.
_last_plan: dict[str, str] = {}


def _run(goal: str = "", confirmed: bool = False, plan: str = "") -> dict:
    wanted = str(goal or "").strip()
    if not wanted:
        return {"ok": False, "error": "Tell me what you'd like done."}
    if not available():
        return {"ok": False, "error": describe_desktop()}

    running = control.active()
    if running is not None and running.as_dict()["active"]:
        return {"ok": False,
                "error": f'I\'m already working on "{running.goal}" on your computer. '
                         "Say stop if you'd like me to leave that."}

    if not confirmed:
        drafted = control.prepare_plan(wanted)
        _last_plan.clear()
        _last_plan[wanted.lower()] = drafted
        return {
            "ok": True,
            "stage": "plan",
            "goal": wanted,
            "plan": drafted or "I'll look at what's on screen and work it out step by step.",
            "note": ("Read this plan back to the user in your own words and ask if you should "
                     "go ahead. Only call this tool again, with confirmed set, after they "
                     "actually say yes."),
        }

    agreed = str(plan or "").strip() or _last_plan.get(wanted.lower(), "")
    try:
        session = control.start(wanted, plan=agreed)
    except RuntimeError as err:
        return {"ok": False, "error": str(err)}
    return {
        "ok": True,
        "stage": "started",
        "sessionId": session.id,
        "note": ("Taking over now. Tell the user in your own words that you're starting and "
                 "to keep hands off the mouse and keyboard — moving the mouse stops you."),
    }


SPEC = CapabilitySpec(
    id="builtin.control_computer",
    name="control_computer",
    description=("Actually operate the computer — click, type, move between windows — to get "
                 "something done, when the user asks for a task to be DONE rather than "
                 "described. Call it once without `confirmed` to get a plan to read back, "
                 "then again with `confirmed` after they agree. To answer a question about "
                 "what's on screen instead, use look_at_screen. " + describe_desktop()),
    input_schema={"type": "object", "properties": {
        "goal": {"type": "string",
                 "description": "What should end up being true, in the user's own terms."},
        "confirmed": {"type": "boolean",
                      "description": "Only true after the user has heard the plan and agreed."},
        "plan": {"type": "string",
                 "description": "The plan they agreed to, as you read it out. Optional — the "
                                "one from your previous call is used if you leave it out."}},
        "required": ["goal"]},
    # The highest tier in the build: never pre-consented, never started by a
    # schedule or a briefing, always a fresh human yes.
    risk=Risk.HIGH,
    handler=_run,
    timeout_s=60.0,
    tags=frozenset({"core"}),
)
