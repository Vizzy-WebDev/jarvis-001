"""The executor — the one place a capability actually runs.

Every guarantee §5 asks for is enforced here rather than left to each caller:
the declared timeout, the retry policy, cancellation, argument validation,
idempotency (§49), argument redaction (§25), and the tool.* events (§38).

The reason it is one place is the audit finding it exists to fix: today the only
tool timeout in the system lives inside the turn runner, so the scheduler,
briefings and the Live voice path invoke tools unbounded, and the allowlist check
sits in that same runner so no other caller enforces it either. A guarantee only
one caller applies is not a guarantee.

**Honesty about timeouts (§45).** Python cannot kill a thread. When a
non-cancellable capability exceeds its timeout, we stop *waiting* — the work may
still be running. The result says `TIMED_OUT` and never `CANCELLED`, and the
message says so plainly, because reporting "I stopped it" when we merely stopped
listening is exactly the fake behaviour §45 forbids. A capability that declares
`cancellable=True` is handed a cancel token and IS asked to stop.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..db import get_db
from ..events import EventType, bus as default_bus
from ..events.bus import EventBus
from ..jscompat import now_iso
from ..policy import Autonomy, CallContext, Outcome, decide
from ..policy import approvals as approvals_store
from ..policy.decide import Grant
from .registry import CapabilityRegistry, registry as default_registry
from .spec import CapabilitySpec

logger = logging.getLogger(__name__)

_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="capability")


class ExecOutcome(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    #: We stopped waiting. For a non-cancellable capability the work may continue.
    TIMED_OUT = "timed_out"
    #: The capability was asked to stop, and it is one that can.
    CANCELLED = "cancelled"
    #: Policy said no, and asking would not change that.
    REFUSED = "refused"
    #: A human has to answer first.
    NEEDS_APPROVAL = "needs_approval"
    #: The arguments did not match the declared schema.
    INVALID_ARGUMENTS = "invalid_arguments"
    #: This operation already ran; the earlier result is returned (§49).
    DUPLICATE = "duplicate"


@dataclass(frozen=True)
class ExecutionResult:
    ok: bool
    outcome: ExecOutcome
    capability: str
    operation_id: str
    value: Any = None
    #: Plain language, safe to show the user verbatim.
    error: str | None = None
    attempts: int = 0
    duration_ms: int = 0
    approval_id: str | None = None
    #: True when a timeout left work possibly still running.
    work_may_continue: bool = False


def _validate(args: dict[str, Any], spec: CapabilitySpec) -> str | None:
    """A deliberately small structural check, not a JSON Schema implementation.

    §47 asks what happens if the model returns invalid tool arguments. The
    answer should be a clear refusal, not a TypeError from inside the handler.
    Required properties and declared types of top-level scalars are checked;
    anything deeper is the handler's own business. No new dependency for this.
    """
    if not isinstance(args, dict):
        return "Arguments must be an object."
    schema = spec.input_schema or {}
    props = schema.get("properties") or {}
    for name in schema.get("required") or []:
        if name not in args:
            return f"Missing required argument: {name}."
    types = {"string": str, "number": (int, float), "integer": int,
             "boolean": bool, "array": list, "object": dict}
    for key, value in args.items():
        declared = (props.get(key) or {}).get("type")
        expected = types.get(declared) if isinstance(declared, str) else None
        if expected and not isinstance(value, expected):
            # bool is a subclass of int; a boolean where a number is wanted is
            # almost always a mistake worth catching.
            if declared in ("number", "integer") and isinstance(value, bool):
                return f"{key} should be a {declared}, not a true/false value."
            if not isinstance(value, expected):
                return f"{key} should be a {declared}."
    return None


def _ask_text(spec: CapabilitySpec, args: dict[str, Any], fallback: str) -> str:
    """What the user is actually asked. The capability's own summary when it has
    one, since only it knows what these arguments mean; the policy's reason
    otherwise."""
    if spec.summarize is None:
        return fallback
    try:
        summary = spec.summarize(args)
    except Exception:  # noqa: BLE001 — a broken summary must not block the gate
        logger.exception("%s failed to summarise its own call", spec.name)
        return fallback
    return summary.strip() if isinstance(summary, str) and summary.strip() else fallback


def _redact(args: dict[str, Any], spec: CapabilitySpec) -> dict[str, Any]:
    if not spec.redact_args:
        return dict(args)
    return {k: ("<redacted>" if k in spec.redact_args else v) for k, v in args.items()}


def _recall(operation_id: str) -> ExecutionResult | None:
    row = get_db().execute(
        "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
    ).fetchone()
    if row is None:
        return None
    return ExecutionResult(
        ok=bool(row["ok"]),
        outcome=ExecOutcome.DUPLICATE,
        capability=row["capability"],
        operation_id=row["operation_id"],
        value=json.loads(row["result"]) if row["result"] else None,
        error=row["error"],
        attempts=row["attempts"],
        duration_ms=row["duration_ms"] or 0,
    )


def _record(result: ExecutionResult, session_id: str | None) -> None:
    try:
        get_db().execute(
            "INSERT OR IGNORE INTO operations (operation_id, capability, session_id, "
            "outcome, ok, result, error, attempts, duration_ms, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                result.operation_id, result.capability, session_id,
                result.outcome.value, 1 if result.ok else 0,
                json.dumps(result.value) if result.value is not None else None,
                result.error, result.attempts, result.duration_ms, now_iso(),
            ),
        )
    except (TypeError, ValueError):
        # A handler returned something unserialisable. Recording the operation
        # still matters more than storing its value, so keep the row and drop
        # the payload rather than losing the idempotency guard.
        get_db().execute(
            "INSERT OR IGNORE INTO operations (operation_id, capability, session_id, "
            "outcome, ok, result, error, attempts, duration_ms, completed_at) "
            "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
            (
                result.operation_id, result.capability, session_id,
                result.outcome.value, 1 if result.ok else 0, result.error,
                result.attempts, result.duration_ms, now_iso(),
            ),
        )


def execute(
    name: str,
    args: dict[str, Any],
    ctx: CallContext,
    *,
    registry: CapabilityRegistry | None = None,
    grants: list[Grant] | None = None,
    allowed_names: frozenset[str] | None = None,
    event_bus: EventBus | None = None,
) -> ExecutionResult:
    """Run a capability, or explain honestly why it did not run."""
    reg = registry or default_registry
    ebus = event_bus or default_bus

    spec = reg.get(name)
    if spec is None:
        return ExecutionResult(
            ok=False, outcome=ExecOutcome.REFUSED, capability=name,
            operation_id=ctx.operation_id,
            error=f"I don't have a capability called {name}.",
        )

    # §49 — a duplicate delivery must not repeat a side effect.
    previous = _recall(ctx.operation_id)
    if previous is not None:
        return previous

    invalid = _validate(args, spec)
    if invalid:
        return ExecutionResult(
            ok=False, outcome=ExecOutcome.INVALID_ARGUMENTS, capability=name,
            operation_id=ctx.operation_id, error=invalid,
        )

    verdict = decide(spec, ctx, grants=grants, allowed_names=allowed_names)
    if verdict.outcome is Outcome.REFUSED:
        return ExecutionResult(
            ok=False, outcome=ExecOutcome.REFUSED, capability=name,
            operation_id=ctx.operation_id, error=verdict.reason,
        )
    if verdict.outcome is Outcome.NEEDS_APPROVAL:
        approval = approvals_store.request(
            spec, args, ctx, _ask_text(spec, args, verdict.reason), event_bus=ebus)
        return ExecutionResult(
            ok=False, outcome=ExecOutcome.NEEDS_APPROVAL, capability=name,
            operation_id=ctx.operation_id, error=verdict.reason,
            approval_id=approval.id,
        )

    return _run(spec, args, ctx, ebus)


def _run(
    spec: CapabilitySpec, args: dict[str, Any], ctx: CallContext, ebus: EventBus
) -> ExecutionResult:
    safe_args = _redact(args, spec)
    ebus.publish(
        EventType.TOOL_STARTED,
        {"capability": spec.name, "args": safe_args, "sessionId": ctx.session_id,
         "operationId": ctx.operation_id},
    )

    started = time.monotonic()
    attempts = 0
    last_error: str | None = None
    total = spec.retry.attempts + 1

    for attempt in range(total):
        attempts = attempt + 1
        cancel = threading.Event()
        future = _pool.submit(_invoke, spec, args, cancel)
        try:
            value = future.result(timeout=spec.timeout_s)
        except FutureTimeout:
            if spec.cancellable:
                cancel.set()
                outcome, may_continue = ExecOutcome.CANCELLED, False
                message = f"{spec.name} took too long, so I stopped it."
            else:
                # §45: do not claim to have stopped something we merely stopped
                # waiting for. Python cannot kill the thread.
                outcome, may_continue = ExecOutcome.TIMED_OUT, True
                message = (
                    f"{spec.name} didn't finish within {spec.timeout_s:.0f}s, so I "
                    "stopped waiting. It may still be running in the background."
                )
            if spec.retry.retry_on_timeout and attempt < total - 1:
                last_error = message
                time.sleep(spec.retry.backoff_s)
                continue
            result = ExecutionResult(
                ok=False, outcome=outcome, capability=spec.name,
                operation_id=ctx.operation_id, error=message, attempts=attempts,
                duration_ms=int((time.monotonic() - started) * 1000),
                work_may_continue=may_continue,
            )
            ebus.publish(
                EventType.TOOL_FAILED,
                {"capability": spec.name, "outcome": outcome.value, "error": message,
                 "sessionId": ctx.session_id, "operationId": ctx.operation_id},
            )
            _record(result, ctx.session_id)
            return result
        except Exception as err:  # noqa: BLE001 — a handler may raise anything
            last_error = str(err) or err.__class__.__name__
            logger.exception("capability %s failed on attempt %d", spec.name, attempts)
            if attempt < total - 1:
                time.sleep(spec.retry.backoff_s)
                continue
            result = ExecutionResult(
                ok=False, outcome=ExecOutcome.FAILED, capability=spec.name,
                operation_id=ctx.operation_id,
                error=f"{spec.name} failed: {last_error}", attempts=attempts,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            ebus.publish(
                EventType.TOOL_FAILED,
                {"capability": spec.name, "outcome": "failed", "error": result.error,
                 "sessionId": ctx.session_id, "operationId": ctx.operation_id},
            )
            _record(result, ctx.session_id)
            return result

        result = ExecutionResult(
            ok=True, outcome=ExecOutcome.COMPLETED, capability=spec.name,
            operation_id=ctx.operation_id, value=value, attempts=attempts,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        ebus.publish(
            EventType.TOOL_COMPLETED,
            {"capability": spec.name, "attempts": attempts,
             "durationMs": result.duration_ms, "sessionId": ctx.session_id,
             "operationId": ctx.operation_id},
        )
        _record(result, ctx.session_id)
        return result

    raise AssertionError("unreachable: the retry loop always returns")


def _invoke(spec: CapabilitySpec, args: dict[str, Any], cancel: threading.Event) -> Any:
    """Call the handler, passing a cancel token only if it declared one."""
    if spec.cancellable:
        return spec.handler(**args, cancel=cancel)
    return spec.handler(**args)


def execute_approved(
    approval_id: str,
    resolving_turn_id: str,
    ctx: CallContext,
    *,
    registry: CapabilityRegistry | None = None,
    event_bus: EventBus | None = None,
) -> ExecutionResult:
    """Run what a now-answered approval authorised.

    Goes through the store's resolve(), so the same-turn refusal applies here
    too — approving and executing cannot both happen inside the turn that asked.
    """
    reg = registry or default_registry
    ebus = event_bus or default_bus

    approval = approvals_store.get(approval_id)
    if approval is None:
        return ExecutionResult(
            ok=False, outcome=ExecOutcome.REFUSED, capability="unknown",
            operation_id=ctx.operation_id, error="That approval no longer exists.",
        )
    resolved = approvals_store.resolve(
        approval_id, approvals_store.Resolution.ALLOW, resolving_turn_id, event_bus=ebus
    )
    if resolved.status is not approvals_store.Resolution.ALLOW:
        return ExecutionResult(
            ok=False, outcome=ExecOutcome.REFUSED, capability=resolved.capability,
            operation_id=approval.operation_id,
            error=f"That was already {resolved.status.value}ed.",
        )

    spec = reg.get(resolved.capability)
    if spec is None:
        return ExecutionResult(
            ok=False, outcome=ExecOutcome.REFUSED, capability=resolved.capability,
            operation_id=approval.operation_id,
            error=f"{resolved.capability} is no longer available.",
        )
    return _run(spec, resolved.args, ctx, ebus)
