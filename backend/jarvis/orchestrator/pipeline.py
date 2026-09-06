"""The orchestrator (§9) — one pipeline, composed rather than absorbed.

    normalize -> route -> [fast path] -> assemble context -> model
              -> tool planning -> policy -> execute -> observe -> respond

Every stage above already exists as its own module: `intent.classify`,
`orchestrator.context`, `capabilities.execute` (which itself asks `policy.decide`),
and `events.bus`. This file wires them together and owns the loop and the
lifecycle — nothing else. It deliberately does NOT import cost tracking, the
self-model, improvement capture, personality or tracing; those subscribe to the
event bus (§38).

That restraint is the whole point. The Node original grew into a 1,027-line file
that is simultaneously the turn loop, the provider gateway and the observability
hub for five subsystems, which is why a second agent loop had to be written from
scratch to get a different perceive step, and why that copy silently lost health
marking, cost capture and the capability seam.

**Honesty about the fast path (§10, §45).** A deterministic route runs the
capability with no model call, and if the capability's own result carries text
fit to say back, that is the whole turn — no generation at all. If it does not,
we do NOT invent a sentence: the result goes into the transcript and the model
answers with it in hand. That still skips a round of tool planning, and it never
pretends to an answer nobody produced.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator

from .. import conversation
from ..assistant.state import AssistantState, State
from ..capabilities import CapabilitySpec, registry as default_registry
from ..capabilities.execute import ExecOutcome, ExecutionResult, execute
from ..capabilities.registry import CapabilityRegistry
from ..events import EventType, bus as default_bus
from ..events.bus import EventBus
from ..intent import Intent, Route, classify
from ..policy import Autonomy, CallContext, Surface
from ..policy.decide import Grant
from .context import AssembledContext, ContextAssembler, WindowContext
from .model_port import ModelClient, ModelSwitched, StepComplete, TextChunk, ToolCall

logger = logging.getLogger(__name__)

#: How many model steps one turn may take before we stop and say so. A loop that
#: silently runs forever is worse than one that admits it is stuck (§47).
MAX_STEPS = 8


# --- what a turn emits -------------------------------------------------------
#
# Typed events rather than dicts, so a consumer that mishandles one fails at the
# call site. The SSE layer translates these to the wire; the tests read them
# directly.

@dataclass(frozen=True)
class Routed:
    intent: Intent
    confidence: float
    reason: str
    fast: bool


@dataclass(frozen=True)
class Chunk:
    text: str


@dataclass(frozen=True)
class ToolRan:
    capability: str
    ok: bool
    outcome: ExecOutcome
    error: str | None = None


@dataclass(frozen=True)
class ApprovalRequired:
    approval_id: str
    capability: str
    reason: str


@dataclass(frozen=True)
class Switched:
    """A model failed mid-reply and another took over — said out loud, because a
    reply that changes course with no explanation is worse than the failure."""

    to_model: str
    reason: str
    from_model: str | None = None


@dataclass(frozen=True)
class Interrupted:
    spoken_text: str


@dataclass(frozen=True)
class Failed:
    error: str


@dataclass(frozen=True)
class Done:
    text: str
    steps: int


TurnEvent = Routed | Chunk | ToolRan | ApprovalRequired | Switched | Interrupted | Failed | Done


@dataclass(frozen=True)
class TurnRequest:
    text: str
    session_id: str
    surface: Surface = Surface.TEXT
    autonomy: Autonomy = Autonomy.INTERACTIVE
    low_confidence: bool = False
    #: Supplied by the caller when a delivery can be repeated (§49): the same
    #: turn id makes every capability call inside it idempotent.
    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    #: An allowlist for restricted work (a job kind, a scheduled task's
    #: connectors). Enforced in the policy layer, not here.
    allowed_names: frozenset[str] | None = None
    grants: list[Grant] | None = None


class Orchestrator:
    def __init__(
        self,
        model: ModelClient,
        *,
        assembler: ContextAssembler | None = None,
        registry: CapabilityRegistry | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._model = model
        self._assembler = assembler or WindowContext()
        self._registry = registry or default_registry
        self._bus = event_bus or default_bus
        self._states: dict[str, AssistantState] = {}
        self._lock = threading.RLock()

    def state_for(self, session_id: str) -> AssistantState:
        with self._lock:
            state = self._states.get(session_id)
            if state is None:
                state = AssistantState(session_id, event_bus=self._bus)
                self._states[session_id] = state
            return state

    # --- the turn ------------------------------------------------------------

    def run_turn(
        self, request: TurnRequest, cancel: threading.Event | None = None
    ) -> Iterator[TurnEvent]:
        """Run one turn, yielding what happened as it happens.

        A generator rather than a callback tree so a caller can stop consuming —
        and so the tests can assert on an exact ordered sequence of events, which
        is what makes the fast path's "no model call happened" claim checkable
        rather than asserted.
        """
        cancel = cancel or threading.Event()
        state = self.state_for(request.session_id)
        text = (request.text or "").strip()

        self._bus.publish(
            EventType.ASSISTANT_INPUT,
            {"sessionId": request.session_id, "turnId": request.turn_id,
             "surface": request.surface.value, "length": len(text)},
        )

        route = classify(text)
        if route.intent is Intent.CLARIFY:
            # Nothing was said. Answering this with a model call would be a model
            # call spent on silence.
            yield Routed(route.intent, route.confidence, route.reason, fast=False)
            yield Done("Sorry — I didn't catch that.", steps=0)
            return

        conversation.push_user_text(request.session_id, text)
        try:
            state.to(State.THINKING, "turn started")
        except Exception:  # noqa: BLE001 — an odd starting state must not lose the turn
            logger.warning("could not enter THINKING from %s", state.state.value)

        fast_spec = self._fast_spec(route)
        yield Routed(route.intent, route.confidence, route.reason, fast=fast_spec is not None)

        try:
            if fast_spec is not None:
                events, needs_model = self._run_fast(request, route, fast_spec, state)
                yield from events
                if not needs_model:
                    return
            yield from self._run_model_loop(request, state, cancel)
        except Exception as err:  # noqa: BLE001 — the turn must never crash the caller
            logger.exception("turn failed")
            state.fail(str(err) or err.__class__.__name__)
            yield Failed(f"Something went wrong on my side: {err}")

    # --- the deterministic path ---------------------------------------------

    def _fast_spec(self, route: Route) -> CapabilitySpec | None:
        """The capability a fast route names, if we actually have it.

        A route naming a capability that is not registered falls through to the
        model rather than failing: the router is a static rule set and the
        registry is populated at runtime, so they can legitimately disagree.
        """
        if route.fast_path is None:
            return None
        return self._registry.get(route.fast_path.capability)

    def _run_fast(
        self,
        request: TurnRequest,
        route: Route,
        spec: CapabilitySpec,
        state: AssistantState,
    ) -> tuple[list[TurnEvent], bool]:
        """Run the one capability a fast route names.

        Returns its events plus whether the model is still needed. Not a
        generator: one capability call is not a stream, and a flag on `self`
        would be shared by every session this orchestrator serves.
        """
        assert route.fast_path is not None
        events: list[TurnEvent] = []
        ctx = self._context_for(request, f"{request.turn_id}:fast")
        state.to(State.EXECUTING, f"fast path: {spec.name}")

        result = execute(
            spec.name, dict(route.fast_path.args), ctx,
            registry=self._registry, grants=request.grants,
            allowed_names=request.allowed_names, event_bus=self._bus,
        )
        events.append(ToolRan(spec.name, result.ok, result.outcome, result.error))

        if result.outcome is ExecOutcome.NEEDS_APPROVAL:
            state.to(State.WAITING_FOR_APPROVAL, f"{spec.name} needs approval")
            events.append(ApprovalRequired(result.approval_id or "", spec.name, result.error or ""))
            return events, False

        conversation.push_assistant_tool_calls(
            request.session_id,
            [{"id": ctx.operation_id, "name": spec.name, "args": dict(route.fast_path.args)}],
        )
        conversation.push_tool_results(
            request.session_id,
            [{"id": ctx.operation_id, "name": spec.name,
              "result": result.value if result.ok else {"error": result.error}}],
        )

        spoken = _spoken_result(result)
        if spoken is None:
            # No sentence to say back. Do not invent one — let the model answer
            # with the result already in the transcript.
            state.to(State.THINKING, "fast path produced data, not an answer")
            return events, True

        conversation.push_assistant_text(request.session_id, spoken)
        self._finish(state, request)
        events.append(Done(spoken, steps=0))
        return events, False

    # --- the model path ------------------------------------------------------

    def _run_model_loop(
        self, request: TurnRequest, state: AssistantState, cancel: threading.Event
    ) -> Iterator[TurnEvent]:
        spoken_so_far: list[str] = []

        for step in range(1, MAX_STEPS + 1):
            if cancel.is_set():
                yield self._interrupt(request, state, "".join(spoken_so_far))
                return

            assembled = self._assemble(request)
            tools = self._declarations(request)

            self._bus.publish(
                EventType.MODEL_CALL_STARTED,
                {"sessionId": request.session_id, "turnId": request.turn_id,
                 "step": step, "tools": len(tools)},
            )
            state.to(State.THINKING, f"step {step}")

            completed: StepComplete | None = None
            try:
                for event in self._model.stream(
                    messages=assembled.messages, system=assembled.system,
                    tools=tools, session_id=request.session_id,
                ):
                    if cancel.is_set():
                        yield self._interrupt(request, state, "".join(spoken_so_far))
                        return
                    if isinstance(event, TextChunk):
                        if event.text:
                            spoken_so_far.append(event.text)
                            yield Chunk(event.text)
                    elif isinstance(event, ModelSwitched):
                        yield Switched(event.to_model, event.reason, event.from_model)
                    elif isinstance(event, StepComplete):
                        completed = event
            except Exception as err:  # noqa: BLE001 — provider errors are expected
                self._bus.publish(
                    EventType.MODEL_CALL_FAILED,
                    {"sessionId": request.session_id, "turnId": request.turn_id,
                     "step": step, "error": str(err)},
                )
                raise

            if completed is None:
                # A client that streamed text but never said the step finished is
                # a broken client, not a silent success.
                raise RuntimeError("the model client ended without completing a step")

            self._bus.publish(
                EventType.MODEL_CALL_COMPLETED,
                {"sessionId": request.session_id, "turnId": request.turn_id,
                 "step": step, "modelId": completed.model_id,
                 "toolCalls": len(completed.tool_calls)},
            )

            if not completed.tool_calls:
                reply = completed.text or "".join(spoken_so_far)
                conversation.push_assistant_text(
                    request.session_id, reply, completed.model_id, completed.raw
                )
                self._bus.publish(
                    EventType.ASSISTANT_RESPONSE,
                    {"sessionId": request.session_id, "turnId": request.turn_id,
                     "steps": step},
                )
                self._finish(state, request)
                yield Done(reply, steps=step)
                return

            conversation.push_assistant_tool_calls(
                request.session_id,
                [{"id": c.id, "name": c.name, "args": c.args} for c in completed.tool_calls],
                model_id=completed.model_id,
                raw=completed.raw,
                text=completed.text or "",
            )

            parked = None
            results: list[dict[str, Any]] = []
            state.to(State.EXECUTING, f"step {step}")
            for call in completed.tool_calls:
                result = self._execute_call(request, call)
                yield ToolRan(call.name, result.ok, result.outcome, result.error)
                results.append({
                    "id": call.id, "name": call.name,
                    "result": result.value if result.ok else {"error": result.error},
                })
                if result.outcome is ExecOutcome.NEEDS_APPROVAL and parked is None:
                    parked = (result, call)

            conversation.push_tool_results(request.session_id, results)

            if parked is not None:
                result, call = parked
                state.to(State.WAITING_FOR_APPROVAL, f"{call.name} needs approval")
                yield ApprovalRequired(result.approval_id or "", call.name, result.error or "")
                return

        # Out of steps. Say so rather than looping or pretending to have finished.
        state.fail("the turn ran out of steps")
        yield Failed(
            f"I went round {MAX_STEPS} times without finishing that, so I stopped. "
            "Tell me what to try differently."
        )

    # --- helpers -------------------------------------------------------------

    def _execute_call(self, request: TurnRequest, call: ToolCall) -> ExecutionResult:
        ctx = self._context_for(request, f"{request.turn_id}:{call.id}")
        return execute(
            call.name, dict(call.args or {}), ctx,
            registry=self._registry, grants=request.grants,
            allowed_names=request.allowed_names, event_bus=self._bus,
        )

    def _context_for(self, request: TurnRequest, operation_id: str) -> CallContext:
        return CallContext(
            session_id=request.session_id,
            turn_id=request.turn_id,
            surface=request.surface,
            autonomy=request.autonomy,
            low_confidence=request.low_confidence,
            operation_id=operation_id,
        )

    def _assemble(self, request: TurnRequest) -> AssembledContext:
        return self._assembler.assemble(session_id=request.session_id, text=request.text)

    def _declarations(self, request: TurnRequest) -> list[dict[str, Any]]:
        specs = self._registry.list()
        if request.allowed_names is not None:
            specs = [s for s in specs if s.name in request.allowed_names]
        return self._registry.declarations(specs)

    def _interrupt(
        self, request: TurnRequest, state: AssistantState, spoken: str
    ) -> Interrupted:
        """Barge-in (§14, §17): keep what was actually said, drop the rest.

        The transcript must reflect what the user HEARD, not what we intended to
        say — otherwise the next turn reasons from a reply that was cut off
        mid-sentence as though it had been delivered in full.
        """
        conversation.mark_last_assistant_interrupted(request.session_id, spoken)
        state.interrupt()
        self._bus.publish(
            EventType.ASSISTANT_INTERRUPTED,
            {"sessionId": request.session_id, "turnId": request.turn_id,
             "spokenChars": len(spoken)},
        )
        return Interrupted(spoken)

    def _finish(self, state: AssistantState, request: TurnRequest) -> None:
        target = State.SPEAKING if request.surface is Surface.VOICE else State.IDLE
        try:
            state.to(target, "reply ready")
        except Exception:  # noqa: BLE001
            logger.warning("could not leave %s at end of turn", state.state.value)


def _spoken_result(result: ExecutionResult) -> str | None:
    """Text from a capability result that is fit to say back, or None.

    Narrow on purpose. A capability that wants to answer a fast-path turn on its
    own says so by returning a string, or a dict with a `speak`/`text` field.
    Anything else is data, and turning data into a sentence is generation — which
    is the model's job, not a formatter's guess.
    """
    if not result.ok:
        return None
    value = result.value
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        for key in ("speak", "text"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return None
