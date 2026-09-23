"""The orchestrator (§9) — one pipeline, composed rather than absorbed.

    normalize -> route -> [fast path] -> assemble context -> model
              -> tool planning -> policy -> execute -> observe -> respond

Every stage above already exists as its own module: `intent.classify`,
`orchestrator.context`, `capabilities.execute` (which itself asks `policy.decide`),
and `events.bus`. This file wires them together and owns the loop and the
lifecycle — nothing else. It deliberately does NOT import cost tracking, the
self-model, improvement capture, or tracing; those subscribe to the event bus
(§38) instead, since they RECORD what happened rather than shape it.

`personality` is the one narrow exception, imported directly rather than via the
bus — it is a dependency-free leaf (see its own header) that only ever turns a
literal `[[laugh]]` token into a `Reaction` event at the exact point the model's
text becomes a `Chunk`; it observes nothing and writes nothing down, so it is
transformation, not the recording the restraint above is about.

That restraint is the whole point. A turn loop that is simultaneously the loop, the model client
and the observability hub for several subsystems forces a second agent loop to be
written from scratch whenever a different perceive step is needed, and that copy silently
loses health marking, cost capture and the capability seam.

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
from dataclasses import dataclass, field, replace
from typing import Any, Iterator

from .. import conversation
from ..assistant.state import TRANSITIONS, AssistantState, State
from ..capabilities import CapabilitySpec, registry as default_registry
from ..capabilities.execute import ExecOutcome, ExecutionResult, execute
from ..capabilities.registry import CapabilityRegistry
from ..events import EventType, bus as default_bus
from ..events.bus import EventBus
from ..intent import Intent, Route, classify
from ..policy import Autonomy, CallContext, Surface
from ..policy.decide import Grant
from .context import AgentBrief, AssembledContext, ContextAssembler, RelevanceContext
from .model_port import (
    ModelClient, ModelSwitched, ModelUnavailable, StepComplete, TextChunk, ToolCall,
)

logger = logging.getLogger(__name__)

#: How many model steps one turn may take before we stop and say so. A loop that
#: silently runs forever is worse than one that admits it is stuck (§47).
MAX_STEPS = 8

#: What 'fast' (how Jarvis spends a turn) lowers that to: fewer rounds of
#: calling a tool and looking at the result before it must answer. A limit on
#: Jarvis's own work, not a statement about any model.
FAST_MAX_STEPS = 4


def step_ceiling() -> int:
    """How many model steps THIS turn may take. Read fresh each turn, so changing
    the setting takes effect on the next message rather than after a restart."""
    from ..prefs import get_prefs

    return FAST_MAX_STEPS if get_prefs().get("balance") == "fast" else MAX_STEPS


#: Added on the last step a turn may take (see `_run_model_loop`).
FINAL_STEP_NOTE = ("This is your last step for this request: there is nothing more to call. "
                   "Answer now from what you have already found, and say plainly what you "
                   "did not get to.")
#: Added to the one extra attempt a turn gets when its last step asked for a tool
#: anyway (see `_run_model_loop`).
INSIST_NOTE = ("No tools exist for this reply — any tool call will be ignored. Write your "
               "answer to the person now, in plain words, from what you already have.")


#: Above this many capabilities, a turn is declared its CORE set plus whatever
#: it has unlocked, rather than everything. Measured, declaring
#: the full set cost ~150,000 characters on every turn — sent on "hello" as much
#: as on anything else — and was the single largest cause of flat,
#: instruction-ignoring replies. The rule is a size rule because the problem is
#: a size problem: a small registry is declared in full, and nothing is hidden
#: from a model that could have held it all anyway.
DECLARATION_BUDGET = 20


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
    #: The just-pushed user message's real, persisted id (chat_store's own
    #: "mN" shape) — never this module's in-memory-only placeholder. Lets the
    #: frontend reference the turn it just showed (for Edit/Retry) without
    #: waiting for a reload to learn what its real id turned out to be. None
    #: for the CLARIFY fast-return, which never gets this far.
    user_message_id: str | None = None


@dataclass(frozen=True)
class Chunk:
    text: str


@dataclass(frozen=True)
class Reaction:
    """A real, audible reaction — never text read aloud as words. Produced by
    `personality.ReactionScanner` from a literal `[[laugh]]` token in the
    model's own streamed text; the token itself never reaches `Chunk`, so it
    never appears in the transcript or gets spoken as words by any voice."""

    kind: str


@dataclass(frozen=True)
class ToolRan:
    capability: str
    ok: bool
    outcome: ExecOutcome
    error: str | None = None
    #: A file the tool produced that the user should SEE — a screenshot, a screen
    #: recording. Carried beside the result rather than inside it because the
    #: model's copy of a tool result is text it reasons over, and an image belongs
    #: in the transcript itself. Shape: {type, kind, url, mimeType}.
    attachment: dict[str, Any] | None = None
    #: A section the interface should open, when the tool's whole job was to take
    #: the user somewhere. Beside the result for the same reason as an
    #: attachment: navigating is something the browser does, not something the
    #: model reasons over. Shape: {section}.
    navigate: dict[str, Any] | None = None
    #: Every attachment, when a tool produced more than one; `attachment` is the
    #: first of them, kept for any reader that only ever shows one.
    attachments: tuple[dict[str, Any], ...] = ()


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
    #: Set when the failure is a KNOWN state rather than a fault — today only
    #: "no model can serve this". A caller showing this to the user needs to
    #: tell those apart: one is worth retrying in ten minutes, the other is a bug.
    code: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Done:
    text: str
    steps: int
    #: Which model actually answered. A scheduled task's run history reports it,
    #: and it is the honest way to tell a pin that was honoured from one that
    #: silently fell back.
    model_id: str | None = None
    #: The reply's own real, persisted id — same reasoning as `Routed.
    #: user_message_id`. None for the CLARIFY fast-return, which never wrote
    #: an assistant message at all.
    message_id: str | None = None


TurnEvent = (Routed | Chunk | Reaction | ToolRan | ApprovalRequired | Switched | Interrupted
            | Failed | Done)


def _one_attachment(action: Any) -> dict[str, Any] | None:
    if not isinstance(action, dict) or not action.get("url"):
        return None
    out = {"type": "attachment", "kind": action.get("kind") or "file",
           "url": str(action["url"]), "mimeType": action.get("mimeType") or ""}
    if action.get("name"):
        # What a file to download is called — an image needs no caption, a file does.
        out["name"] = str(action["name"])
    return out


def _attachments_of(value: Any) -> list[dict[str, Any]]:
    """The `ui_action` attachments on a tool result, if it named any.

    One reader, so a tool announces something to show by returning it — never by
    knowing anything about the turn stream. Either one (`type: attachment`) or
    several at once (`type: attachments`, `items: [...]`) — a specialist asked to
    make a worksheet and its answer key hands both back from one delegation.
    """
    if not isinstance(value, dict):
        return []
    action = value.get("ui_action")
    if not isinstance(action, dict):
        return []
    if action.get("type") == "attachment":
        one = _one_attachment(action)
        return [one] if one else []
    if action.get("type") == "attachments" and isinstance(action.get("items"), list):
        return [a for a in (_one_attachment(item) for item in action["items"]) if a]
    return []


def _attachment_of(value: Any) -> dict[str, Any] | None:
    found = _attachments_of(value)
    return found[0] if found else None


def _navigate_of(value: Any) -> dict[str, Any] | None:
    """A section a tool asked the interface to open.

    Same one-reader shape as `_attachment_of` above: a tool says where to go by
    returning it, and knows nothing about the turn stream or the browser.
    """
    if not isinstance(value, dict):
        return None
    action = value.get("ui_action")
    if not isinstance(action, dict) or action.get("type") != "navigate":
        return None
    section = action.get("section")
    return {"section": str(section)} if section else None


#: Which kind of job the model client should treat this turn as, by where it came
#: from. Plain strings because the port is deliberately neutral (`model_port.py`).
#:
#: This mapping is the whole of what the switchover needed from the turn loop,
#: and it closes a gap rather than adding a feature: before it, every turn
#: reached the router as a default text conversation however it had started, so
#: a spoken reply and an overnight scheduled task were ranked identically.
ROLE_FOR_SURFACE: dict[Surface, str] = {
    Surface.TEXT: "conversation",
    Surface.VOICE: "voice",
    Surface.CONTROL: "control",
    Surface.JOB: "background",
    Surface.SCHEDULED: "background",
}


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
    #: A one-off model pin — a scheduled task naming the model it wants. Passed to the
    #: model client as-is, which resolves it against the connected models
    #: (`models/selection.py`): found or refused, never approximated.
    model_id: str | None = None
    grants: list[Grant] | None = None
    #: Upload ids attached to this turn. Ids, never paths: what arrives from the
    #: browser is untrusted, and resolving one is the upload store's job.
    attachments: tuple[str, ...] = ()
    #: Set when this turn is run AS a specialist agent rather than as Jarvis: its
    #: identity and doctrine replace Jarvis's in the prompt. Plain data built by
    #: `agents/runner.py` — this loop never imports the agents package.
    agent: AgentBrief | None = None
    #: Under the declaration budget, the names declared up front besides the core
    #: set. Defaults to `allowed_names`; an agent sets it apart because what it is
    #: PERMITTED (every connector tool, say) can be far more than is worth
    #: DECLARING on every step — the rest stay reachable through find_capability.
    always_declare: frozenset[str] | None = None


def _prepare_for_new_turn(state: AssistantState) -> None:
    """A new turn must always be able to start, whatever the assistant's
    per-session state was left at by whatever happened before it.

    Nothing ever signals this machine "the reply finished playing" — a voice
    turn ends with it parked in SPEAKING (see `_finish`) and it just stays
    there — so the next turn's attempt to enter THINKING is illegal on its
    face. The same shape of problem hits ERROR: any turn that fails leaves the
    session there, and ERROR's only legal exits are IDLE/LISTENING, not
    THINKING, so a session with one failed turn would crash on every turn
    after it, forever, with nothing to recover it.

    Walk the real intermediate hop the lifecycle defines for the common case
    (a voice reply finishing and a follow-up arriving is SPEAKING -> LISTENING
    -> TRANSCRIBING — "conversation mode", per the transition table's own
    comment), and fall back to reset() — always legal, and exactly what it
    exists for — for anything else with no defined resume path.
    """
    if State.THINKING in TRANSITIONS[state.state]:
        return
    if state.state is State.SPEAKING:
        state.to(State.LISTENING, "new turn arrived while still marked speaking")
        state.to(State.TRANSCRIBING, "new turn arrived while still marked speaking")
        return
    state.reset()


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
        self._assembler = assembler or RelevanceContext()
        self._registry = registry or default_registry
        self._bus = event_bus or default_bus
        self._states: dict[str, AssistantState] = {}
        #: What each in-flight turn's attachments require, keyed by turn id. Set
        #: when the attachments are prepared and read once by the model loop.
        self._needs: dict[str, dict[str, bool]] = {}
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

        # `text` rides in this event (not just `length`, its only field before)
        # specifically so Self-Improvement's capture step can subscribe rather
        # than the turn loop importing it directly — this file deliberately
        # does not import cost tracking, the self-model, improvement capture,
        # personality or tracing; those subscribe to the event bus instead.
        self._bus.publish(
            EventType.ASSISTANT_INPUT,
            {"sessionId": request.session_id, "turnId": request.turn_id,
             "surface": request.surface.value, "length": len(text), "text": text},
        )

        route = classify(text)
        if route.intent is Intent.CLARIFY and not request.attachments:
            # Nothing was said and nothing was attached either. Answering this
            # with a model call would be a model call spent on silence. An
            # attachment-only send (empty text, a real file) must NOT hit this —
            # classify() only ever looks at text, so it has no way to know
            # attachments exist; that check belongs here instead.
            yield Routed(route.intent, route.confidence, route.reason, fast=False)
            yield Done("Sorry — I didn't catch that.", steps=0)
            return

        # Attachments are prepared BEFORE the message is pushed: what the model
        # sees has to include them, and a document read as text has to be part
        # of the message itself rather than an aside.
        prepared = self._attachments(request)
        message_text = text
        if prepared is not None:
            from ..attachments import compose_message

            message_text = compose_message(text, prepared)
        pushed_user = conversation.push_user_text(request.session_id, message_text,
                                                   media=prepared.media if prepared else None)
        _prepare_for_new_turn(state)
        state.to(State.THINKING, "turn started")

        fast_spec = self._fast_spec(route)
        yield Routed(route.intent, route.confidence, route.reason, fast=fast_spec is not None,
                     user_message_id=pushed_user.get("id"))

        try:
            if fast_spec is not None:
                events, needs_model = self._run_fast(request, route, fast_spec, state)
                yield from events
                if not needs_model:
                    return
            yield from self._run_model_loop(request, state, cancel)
        except ModelUnavailable as err:
            # Not a fault: the honest state of the model roster, with the real
            # reasons and the soonest retry already in the message.
            state.fail(str(err))
            yield Failed(str(err), code="no_model", detail=err.detail)
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
        events.append(ToolRan(spec.name, result.ok, result.outcome, result.error,
                              _attachment_of(result.value), _navigate_of(result.value),
                              tuple(_attachments_of(result.value))))

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

        pushed_reply = conversation.push_assistant_text(request.session_id, spoken)
        self._finish(state, request)
        events.append(Done(spoken, steps=0, message_id=pushed_reply.get("id")))
        return events, False

    # --- the model path ------------------------------------------------------

    def _run_model_loop(
        self, request: TurnRequest, state: AssistantState, cancel: threading.Event
    ) -> Iterator[TurnEvent]:
        # What the attachments on this turn require of a model. Computed once and
        # passed on every step: an image in the transcript still needs a model
        # that can see it three steps later.
        needs = dict(self._needs.pop(request.turn_id, {}))
        spoken_so_far: list[str] = []
        # What this turn has been granted beyond the core set, by a capability
        # that returned `unlock` (see tools/find_capability.py). Per-turn and
        # per-call: nothing here outlives the turn that earned it.
        unlocked: set[str] = set()
        # What this turn actually DID, for the observers downstream of it. The
        # loop keeps the list; it does not know or care who reads it.
        tools_used: list[str] = []
        ceiling = step_ceiling()
        # Set when the last step came back asking for a tool it was not offered:
        # that earns exactly one more attempt at the answer (see below).
        insist = False

        for step in range(1, ceiling + 2):
            if step > ceiling and not insist:
                break
            if cancel.is_set():
                yield self._interrupt(request, state, "".join(spoken_so_far))
                return

            assembled = self._assemble(request)
            tools = self._declarations(request, unlocked)
            system = assembled.system
            final = step >= ceiling and ceiling > 1
            if final:
                # The last step this turn gets. Found live: a specialist made 22
                # successful lookups across its steps, never wrote its answer, and
                # everything it had found was thrown away with "I went round 8
                # times". On the last step it is asked to answer, with nothing to
                # call — the work it already did is still in front of it.
                tools = []
                system = f"{system}\n\n{FINAL_STEP_NOTE}"
                if step > ceiling:
                    system = f"{system}\n\n{INSIST_NOTE}"

            self._bus.publish(
                EventType.MODEL_CALL_STARTED,
                {"sessionId": request.session_id, "turnId": request.turn_id,
                 "step": step, "tools": len(tools)},
            )
            state.to(State.THINKING, f"step {step}")

            completed: StepComplete | None = None
            # Scoped to this one step — a marker split across a tool-call
            # boundary would be meaningless anyway, since a new step is a new
            # generation, not a continuation of the same text stream.
            from ..personality import create_reaction_scanner, strip_reaction_markers

            reactions = create_reaction_scanner()
            try:
                for event in self._model.stream(
                    messages=assembled.messages, system=system,
                    tools=tools, session_id=request.session_id,
                    model_id=request.model_id,
                    role=ROLE_FOR_SURFACE.get(request.surface, "conversation"),
                    need=needs or None,
                ):
                    if cancel.is_set():
                        # Whatever the scanner is still holding back (at most a
                        # few characters — see its own docstring) was genuinely
                        # heard before the user cut in; it just hadn't yet been
                        # decided to be ordinary text rather than the start of a
                        # marker. Yielded as a real Chunk too, so what gets
                        # recorded as "heard" matches what was actually sent.
                        for piece in reactions.flush():
                            if piece.text:
                                spoken_so_far.append(piece.text)
                                yield Chunk(piece.text)
                        yield self._interrupt(request, state, "".join(spoken_so_far))
                        return
                    if isinstance(event, TextChunk):
                        if event.text:
                            for piece in reactions.feed(event.text):
                                if piece.type == "reaction":
                                    yield Reaction(piece.kind)
                                elif piece.text:
                                    spoken_so_far.append(piece.text)
                                    yield Chunk(piece.text)
                    elif isinstance(event, ModelSwitched):
                        yield Switched(event.to_model_id, event.reason, event.from_model_id)
                    elif isinstance(event, StepComplete):
                        completed = event
            except Exception as err:  # noqa: BLE001 — provider errors are expected
                self._bus.publish(
                    EventType.MODEL_CALL_FAILED,
                    {"sessionId": request.session_id, "turnId": request.turn_id,
                     "step": step, "error": str(err)},
                )
                raise

            # Anything still held back was never a real marker, just ordinary
            # text that happened to look like the start of one.
            for piece in reactions.flush():
                if piece.text:
                    spoken_so_far.append(piece.text)
                    yield Chunk(piece.text)

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

            if final and completed.tool_calls:
                # Offered nothing to call, it asked to call something anyway — found
                # live: some models copy the tool-calling pattern from earlier in the
                # conversation. Nothing is run, and it gets ONE more, firmer, attempt.
                # Words written beside a call are usually just its lead-in ("Let me
                # also try the markets page…" — found live, delivered as a whole
                # specialist's result), so they count as the answer only when the
                # firmer attempt still produces nothing better; with no words at all,
                # the honest "went round" failure below.
                if step == ceiling:
                    insist = True
                    continue
                if strip_reaction_markers(completed.text).strip():
                    completed = replace(completed, tool_calls=())
                else:
                    break

            if not completed.tool_calls:
                # `completed.text` is the model client's own final assembly, built
                # independently of the streamed Chunks above — it can still
                # carry a raw [[laugh]] token the scanner never saw, so it
                # gets the same stripping before it ever reaches the
                # transcript or gets said back as words.
                reply = strip_reaction_markers(completed.text) or "".join(spoken_so_far)
                pushed_reply = conversation.push_assistant_text(
                    request.session_id, reply, completed.model_id, completed.raw
                )
                self._bus.publish(
                    EventType.ASSISTANT_RESPONSE,
                    {"sessionId": request.session_id, "turnId": request.turn_id,
                     "steps": step, "text": reply, "userText": request.text,
                     "toolNames": list(tools_used)},
                )
                self._finish(state, request)
                yield Done(reply, steps=step, model_id=completed.model_id,
                          message_id=pushed_reply.get("id"))
                return

            conversation.push_assistant_tool_calls(
                request.session_id,
                [{"id": c.id, "name": c.name, "args": c.args} for c in completed.tool_calls],
                model_id=completed.model_id,
                raw=completed.raw,
                text=strip_reaction_markers(completed.text) or "",
            )

            parked = None
            results: list[dict[str, Any]] = []
            state.to(State.EXECUTING, f"step {step}")
            for call in completed.tool_calls:
                result = self._execute_call(request, call)
                tools_used.append(call.name)
                yield ToolRan(call.name, result.ok, result.outcome, result.error,
                              _attachment_of(result.value), _navigate_of(result.value),
                              tuple(_attachments_of(result.value)))
                results.append({
                    "id": call.id, "name": call.name,
                    "result": result.value if result.ok else {"error": result.error},
                })
                unlocked |= _unlocked_by(result.value)
                if parked is None:
                    if result.outcome is ExecOutcome.NEEDS_APPROVAL:
                        parked = ApprovalRequired(result.approval_id or "", call.name,
                                                  result.error or "")
                    else:
                        parked = _approval_of(result.value)

            conversation.push_tool_results(request.session_id, results)

            if parked is not None:
                state.to(State.WAITING_FOR_APPROVAL, f"{parked.capability} needs approval")
                yield parked
                return

        # Out of steps. Say so rather than looping or pretending to have finished.
        state.fail("the turn ran out of steps")
        yield Failed(
            f"I went round {ceiling} times without finishing that, so I stopped. "
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

    def _attachments(self, request: TurnRequest) -> Any:
        """Read this turn's attachments, or None when there are none.

        A failure here never fails the turn: the user still asked something, and
        "I couldn't read that file" is a better answer than no answer at all.
        """
        if not request.attachments:
            return None
        from ..attachments import prepare_for_turn

        try:
            prepared = prepare_for_turn(list(request.attachments),
                                        session_id=request.session_id)
        except Exception:  # noqa: BLE001
            logger.exception("could not prepare attachments for %s", request.turn_id)
            return None
        if prepared.need:
            self._needs[request.turn_id] = prepared.need
        return prepared

    def _assemble(self, request: TurnRequest) -> AssembledContext:
        # Passed only when set, so an assembler written before agents existed
        # (a test's own, say) keeps working unchanged.
        extra = {"agent": request.agent} if request.agent is not None else {}
        return self._assembler.assemble(
            session_id=request.session_id, text=request.text,
            low_confidence=request.low_confidence,
            # A job's or a scheduled task's own turn has nobody to tell: what is
            # waiting for the user belongs in a turn the user actually started.
            background=request.surface in (Surface.JOB, Surface.SCHEDULED), **extra)

    def _declarations(self, request: TurnRequest, unlocked: set[str]) -> list[dict[str, Any]]:
        specs = self._registry.list()
        # A tool tagged `job` is a background worker's own voice — reporting on
        # itself, asking for its work to be split. It reaches a job's turn and
        # nothing else: a live conversation that could call one would be talking
        # about a job as if it were the job.
        if request.surface is not Surface.JOB:
            specs = [s for s in specs if not s.has_tag("job")]
        if request.allowed_names is not None:
            specs = [s for s in specs if s.name in request.allowed_names]
        if len(specs) > DECLARATION_BUDGET:
            # An explicit allowlist counts as unlocking what it names: naming a
            # non-core tool for a restricted task and then not declaring it is
            # how a job ends up unable to do the one thing it was created for.
            allowed = (request.always_declare if request.always_declare is not None
                       else request.allowed_names or frozenset())
            specs = [s for s in specs
                     if s.has_tag("core") or s.name in unlocked or s.name in allowed]
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


def _approval_of(value: Any) -> ApprovalRequired | None:
    """A question a DELEGATED turn stopped on, handed up in a tool result.

    A specialist's run cannot ask the person itself — it stops at the question,
    exactly as a turn does, and `ask_specialist` returns it as plain data. This
    turn then puts it in front of the person as a real approval, so it is answered
    by them, later, through the same gate as every other one. Same one-reader
    shape as `_attachment_of`: the tool knows nothing about the turn stream.
    """
    if not isinstance(value, dict):
        return None
    approval = value.get("approval")
    if not isinstance(approval, dict) or not approval.get("id"):
        return None
    agent = value.get("agent")
    reason = str(approval.get("reason") or "")
    if agent:
        reason = f"{agent} needs your go-ahead: {reason}" if reason else f"{agent} needs your go-ahead."
    return ApprovalRequired(str(approval["id"]), str(approval.get("capability") or ""), reason)


def _unlocked_by(value: Any) -> set[str]:
    """Capability names a tool result asks to make callable for the rest of this
    turn. Plain data in a result, so no tool needs a handle on the turn loop."""
    if not isinstance(value, dict):
        return set()
    names = value.get("unlock")
    if not isinstance(names, (list, tuple)):
        return set()
    return {n for n in names if isinstance(n, str) and n}


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
