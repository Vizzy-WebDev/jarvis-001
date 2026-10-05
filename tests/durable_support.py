"""What the durable-work tests share: a model that answers from the transcript it is sent,
effects counted in a file, a simulated crash, and a "restart" of the process's memory.

**The model answers from what it is sent, never from call order.** A round replayed after a
crash is sent the same transcript, so it gets the same answers — exactly what a real model
given the same context would be asked again. Its position is read off the transcript itself:
the round is how many user messages there are, the step how many assistant messages follow
the last one. Every call is kept (`calls`), so "this round's model calls were not repeated" is
a count, not a belief.

**Effects are counted, not inferred.** `effect_low` (LOW risk) and `effect_external` (MEDIUM
risk) append one line per real execution to a file in the scratch data dir; "exactly once"
means that file has exactly one line for that tag.

**A crash is a `SimulatedCrash`** — a `BaseException`, so nothing on the way up catches it the
way an ordinary failure is caught (the executor, the turn loop and LangGraph all let it
through, exactly as they would let a process die). `restart()` then throws away what a dead
process would lose: the in-memory sessions, the assembled orchestrator, the checkpoint
connection. What is left is what was on disk.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, Iterator

from jarvis.capabilities import CapabilitySpec, Risk
from jarvis.orchestrator.model_port import StepComplete, TextChunk, ToolCall


class SimulatedCrash(BaseException):
    """The process died here."""


# --- the model -----------------------------------------------------------------------------

Step = tuple  # ("say", text) | ("call", name, args)


def position(messages: list[dict[str, Any]]) -> tuple[int, int]:
    """(round, step) from the transcript: user messages so far − 1, and assistant messages
    after the last user message."""
    users = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    if not users:
        return (0, 0)
    after = [m for m in messages[users[-1] + 1:] if m.get("role") == "assistant"]
    return (len(users) - 1, len(after))


class Brain:
    """A scripted model keyed on (session prefix, round, step).

    `script[round][step]` is what it does at that point; past the end of a round's list it
    answers "done with this round." (a final answer). `final_text` decides what a plain answer
    says. `crash_at` raises `SimulatedCrash` the FIRST time a given (session, round, step) is
    reached — once, like a process that died once.
    """

    def __init__(self, script: dict[str, list[list[Step]]] | None = None) -> None:
        self.script = script or {}
        self.calls: list[dict[str, Any]] = []
        self.crash_at: set[tuple[str, int, int]] = set()
        self.hold: dict[tuple[str, int, int], threading.Event] = {}
        self.reached: dict[tuple[str, int, int], threading.Event] = {}
        self._lock = threading.Lock()

    def plan(self, session_prefix: str, rounds: list[list[Step]]) -> "Brain":
        self.script[session_prefix] = rounds
        return self

    def _script_for(self, session_id: str) -> list[list[Step]]:
        for prefix, rounds in self.script.items():
            if session_id.startswith(prefix):
                return rounds
        return []

    def calls_for(self, session_id: str, round_no: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            return [c for c in self.calls if c["session"] == session_id
                    and (round_no is None or c["round"] == round_no)]

    def stream(self, *, messages: list[dict[str, Any]], system: str, tools: list[dict[str, Any]],
               session_id: str, model_id: str | None = None, role: str | None = None,
               need: dict[str, bool] | None = None) -> Iterator[Any]:
        round_no, step = position(messages)
        key = (session_id, round_no, step)
        with self._lock:
            self.calls.append({"session": session_id, "round": round_no, "step": step,
                               "system": system, "tools": [t.get("name") for t in tools],
                               "messages": messages})
            crash = key in self.crash_at
            self.crash_at.discard(key)
        if key in self.reached:
            self.reached[key].set()
        if key in self.hold:
            assert self.hold[key].wait(30), f"held step {key} was never released"
        if crash:
            raise SimulatedCrash(f"crash at {key}")

        rounds = self._script_for(session_id)
        steps = rounds[round_no] if round_no < len(rounds) else []
        planned: Step = steps[step] if step < len(steps) else ("say", f"done with round {round_no}.")
        if planned[0] == "call" and tools:
            name, args = planned[1], planned[2]
            call = ToolCall(id=f"c{round_no}_{step}", name=name, args=dict(args))
            yield StepComplete(tool_calls=(call,), finish_reason="tool_calls", model_id="brain")
            return
        text = planned[1] if planned[0] == "say" else f"(no tools offered) round {round_no}."
        yield TextChunk(text)
        yield StepComplete(text=text, model_id="brain")


def install(brain: Brain) -> Brain:
    from jarvis import assembly
    from jarvis.events.bus import bus
    from jarvis.orchestrator import Orchestrator

    assembly._orchestrator = Orchestrator(brain, registry=assembly.get_registry(), event_bus=bus)
    return brain


# --- counted effects -----------------------------------------------------------------------

def counter_file() -> Path:
    from jarvis.store import data_dir

    return data_dir() / "effects.log"


def effect_count(tag: str | None = None) -> int:
    path = counter_file()
    if not path.exists():
        return 0
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    return len(lines) if tag is None else sum(1 for line in lines if line == tag)


#: Crash hooks for the effect handlers: "before" (the handler started, nothing written yet)
#: and "after" (the line is written, the result not yet returned/recorded), by tag.
crash_effect: dict[str, str] = {}


def _effect(tag: str = "x") -> dict[str, Any]:
    if crash_effect.get(tag) == "before":
        crash_effect.pop(tag)
        raise SimulatedCrash(f"crash before effect {tag}")
    with counter_file().open("a", encoding="utf-8") as fh:
        fh.write(tag + "\n")
    if crash_effect.get(tag) == "after":
        crash_effect.pop(tag)
        raise SimulatedCrash(f"crash after effect {tag}")
    return {"done": tag}


def _effect_spec(name: str, risk: Risk) -> CapabilitySpec:
    return CapabilitySpec(
        id=name, name=name,
        description=f"Test effect ({risk.value} risk): writes one line per run.",
        input_schema={"type": "object", "properties": {"tag": {"type": "string"}},
                      "required": ["tag"]},
        handler=_effect, risk=risk, tags=frozenset({"core"}))


def look_up(q: str = "") -> dict[str, Any]:
    return {"found": f"facts about {q}"}


def register_effects() -> None:
    """Add the counted effects (and a plain lookup) to the real registry."""
    from jarvis import assembly

    registry = assembly.get_registry()
    for spec in (_effect_spec("effect_low", Risk.LOW), _effect_spec("effect_external", Risk.MEDIUM),
                 _effect_spec("effect_high", Risk.HIGH),
                 CapabilitySpec(id="look_up_fact", name="look_up_fact",
                                description="Look up a fact (test).",
                                input_schema={"type": "object",
                                              "properties": {"q": {"type": "string"}}},
                                handler=look_up, risk=Risk.LOW, tags=frozenset({"core"}))):
        if registry.get(spec.name) is None:
            registry.register(spec)


# --- the restart ---------------------------------------------------------------------------

def restart(brain: Brain | None = None) -> None:
    """Lose what a dead process loses; keep what is on disk. The brain (a stand-in for a
    remote model) survives, as a provider would."""
    from jarvis import assembly, conversation, durable

    conversation.reset_for_tests()
    assembly.reset_for_tests()
    durable.reset_for_tests()
    register_effects()
    if brain is not None:
        install(brain)


def run_and_wait(fn: Callable[[], Any], timeout: float = 30.0) -> Any:
    """Run `fn` on a thread with a hard deadline: a hang fails the test, never waits it out.
    A `SimulatedCrash` raised inside comes back as the result (the process "died")."""
    box: dict[str, Any] = {}

    def go() -> None:
        try:
            box["value"] = fn()
        except BaseException as err:  # noqa: BLE001 — a simulated crash is a result here
            box["error"] = err

    thread = threading.Thread(target=go, daemon=True)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), f"did not finish within {timeout}s"
    if "error" in box and not isinstance(box["error"], SimulatedCrash):
        raise box["error"]
    return box.get("value", box.get("error"))
