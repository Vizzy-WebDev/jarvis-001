"""Execute: run a request over its ranked endpoints.

* A retryable error (rate_limited, unavailable, timeout) is retried on the same
  endpoint with a short backoff, then the call falls back to the next endpoint
  from the already-filtered list — preferring one with a different upstream.
  Hard requirements and data policy are never relaxed: nothing outside that list
  is ever tried.
* Any other error ends the call.
* A fallback that would change model family is skipped when the request says
  `allow_family_change: false`.
* Streaming: fall back only before the first content event. After that, an error
  event, and stop.
* Every connection's concurrency and requests-per-minute limits are kept, its
  rate-limit rest is set after a 429, each endpoint's circuit breaker is fed,
  and what a call cost is added to the month's spend.
"""

from __future__ import annotations

import logging
import secrets as token
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Generator, Iterator

from .. import config as app_secrets
from . import adapt, drivers, engine, finish, state
from . import config as layer_config
from .errors import ModelError, RateLimited, SchemaValidationFailed, Unavailable
from .finish import Assembled
from .prepared import ConnInfo
from .resolve import Candidate
from .types import (CONTENT_EVENTS, Attempt, Done, ErrorEvent, Event, Fallback, FeatureReport, Finish, Message,
                    Provenance, Rejection, Request, Response, TextDelta, TextPart, ToolCall, Usage)

logger = logging.getLogger(__name__)

#: A server asking us to wait longer than this is not retried: the call moves on.
LONG_WAIT_S = 10.0
#: How long a connection rests after a 429 that didn't say how long.
DEFAULT_RATE_REST_S = 20.0

#: The clock and the pause, named so tests drive time instead of waiting for it.
clock: Callable[[], float] = time.monotonic
sleep: Callable[[float], None] = time.sleep

#: Called with every finished call's trace record (see trace.py).
observers: list[Callable[["TraceRecord"], None]] = []


def mint_id() -> str:
    return f"call_{token.token_hex(12)}"


# --- per-connection limits ----------------------------------------------------------------------

_limits_lock = threading.Lock()
_semaphores: dict[tuple[str, int], threading.BoundedSemaphore] = {}
_windows: dict[str, deque[float]] = {}


@contextmanager
def _slot(candidate: Candidate) -> Iterator[None]:
    limits = candidate.connection.limits
    sem = None
    if limits.concurrency:
        with _limits_lock:
            sem = _semaphores.setdefault((candidate.connection.name, limits.concurrency),
                                         threading.BoundedSemaphore(limits.concurrency))
        sem.acquire()
    try:
        if limits.rpm:
            while True:
                with _limits_lock:
                    window = _windows.setdefault(candidate.connection.name, deque())
                    now = clock()
                    while window and now - window[0] >= 60.0:
                        window.popleft()
                    if len(window) < limits.rpm:
                        window.append(now)
                        break
                    wait = 60.0 - (now - window[0])
                sleep(max(wait, 0.05))
        yield
    finally:
        if sem is not None:
            sem.release()


# --- the trace record ---------------------------------------------------------------------------

@dataclass
class TraceRecord:
    request_id: str
    task_class: str
    affinity_key: str | None
    data_class: str
    ranked: list[str]
    rejected: list[Rejection]
    attempts: list[Attempt] = field(default_factory=list)
    fallbacks: list[Fallback] = field(default_factory=list)
    endpoint_id: str | None = None
    reported_model: str | None = None
    ttft_ms: int | None = None
    total_ms: int | None = None
    usage: Usage | None = None
    report: FeatureReport | None = None
    outcome: str = "ok"  # 'ok' | an error type
    signals: dict[str, Any] = field(default_factory=dict)
    request: Request | None = None
    response: Response | None = None


def _publish(record: TraceRecord) -> None:
    for fn in list(observers):
        try:
            fn(record)
        except Exception:  # noqa: BLE001 - recording must never cost anyone their answer
            logger.exception("a model-call observer failed")


# --- one endpoint, once --------------------------------------------------------------------------

def _conn_info(candidate: Candidate, cfg: layer_config.Config) -> ConnInfo:
    conn = candidate.connection
    key = app_secrets.get_secret(conn.secret_ref) if conn.secret_ref else None
    return ConnInfo(name=conn.name, base_url=conn.base_url, api_key=key, quirks=dict(cfg.quirks_of(conn).wire))


@dataclass
class _Progress:
    emitted: bool = False
    first_at: float | None = None


def _drive(candidate: Candidate, prepared: Any, cfg: layer_config.Config, progress: _Progress, *,
           emit: bool, started: float) -> Generator[Event, None, Assembled]:
    driver = drivers.get(candidate.connection.driver)
    assembled = Assembled()
    endpoint_id = candidate.endpoint.id
    try:
        with _slot(candidate):
            for event in driver.stream(_conn_info(candidate, cfg), prepared):
                if isinstance(event, CONTENT_EVENTS):
                    if progress.first_at is None:
                        progress.first_at = clock()
                    if emit:
                        progress.emitted = True
                        yield event
                assembled.add(event, endpoint_id)
                if isinstance(event, Finish):
                    break
    except ModelError:
        raise
    except Exception as err:  # noqa: BLE001 - a reply the driver couldn't read is a failure, said plainly
        logger.exception("the %s driver couldn't read a reply", candidate.connection.driver)
        raise Unavailable(f"{candidate.connection.label or candidate.connection.name} sent a reply Jarvis "
                          "couldn't read.") from err
    if assembled.finish is None:
        raise Unavailable("The reply stopped part-way.")
    return assembled


def _one(request: Request, candidate: Candidate, cfg: layer_config.Config, report: adapt.Report,
         progress: _Progress, record: TraceRecord, *, streaming: bool) -> Generator[Event, None, tuple[Assembled, Usage, Any]]:
    started = clock()
    prepared = adapt.prepare(request, candidate, cfg, mint_id=mint_id, report=report)
    emit = streaming and not candidate.emulated
    assembled = yield from _drive(candidate, prepared, cfg, progress, emit=emit, started=started)
    usage = finish.cost_of(candidate.endpoint, assembled.finish.usage if assembled.finish else Usage())
    data = None

    if request.output.kind == "json":
        schema = candidate.output_schema
        data, problems = finish.check_output(assembled.text, schema)
        repairs = 0
        while problems and candidate.emulated and repairs < cfg.settings.repair_attempts:
            repairs += 1
            record.signals["schema_repair_used"] = repairs
            fix = (f"Your reply didn't match the required JSON Schema: {'; '.join(problems)}. "
                   "Reply again with only the corrected JSON object.")
            retry_items = prepared.items + tuple(assembled.items) + (Message("user", (TextPart(fix),)),)
            again = type(prepared)(**{**prepared.__dict__, "items": retry_items})
            assembled = yield from _drive(candidate, again, cfg, progress, emit=False, started=started)
            usage = finish.add_usage(usage, finish.cost_of(candidate.endpoint, assembled.finish.usage))
            data, problems = finish.check_output(assembled.text, schema)
        if problems:
            record.signals["schema_repair_failed"] = True
            how = "after asking again" if candidate.emulated and repairs else "from a model that was meant to enforce it"
            raise SchemaValidationFailed(f"The reply didn't match the required format {how}: {'; '.join(problems)}")
        if repairs:
            report.warnings.append(f"The reply needed {repairs} correction{'s' if repairs != 1 else ''} to match "
                                   "the required format.")
        if streaming and candidate.emulated and assembled.text:
            progress.emitted = True
            if progress.first_at is None:
                progress.first_at = clock()
            yield TextDelta(assembled.text)

    for item in assembled.items:
        if isinstance(item, ToolCall) and item.arguments is None:
            record.signals["invalid_tool_arguments"] = record.signals.get("invalid_tool_arguments", 0) + 1
            report.warnings.append(f"The model's arguments for “{item.name}” weren't valid JSON; "
                                   "they are returned exactly as sent.")
    return assembled, usage, data


# --- the whole call -------------------------------------------------------------------------------

def _upstream(candidate: Candidate) -> str:
    return candidate.endpoint.upstream or f"connection:{candidate.connection.name}"


def _same_family(a: Candidate, b: Candidate) -> bool:
    return bool(a.endpoint.family) and a.endpoint.family == b.endpoint.family


def _note_failure(candidate: Candidate, err: ModelError, cfg: layer_config.Config) -> None:
    """The only use of an error's kind outside the call itself: the connection's
    rate-limit rest and the endpoint's breaker."""
    if isinstance(err, RateLimited):
        state.rate_limit(candidate.connection.name, err.retry_after or DEFAULT_RATE_REST_S)
    elif err.type in ("unavailable", "timeout", "auth"):
        s = cfg.settings
        state.record_failure(candidate.endpoint.id, str(err), threshold=s.breaker_threshold,
                             base_s=s.breaker_base_s, max_s=s.breaker_max_s)


def run(request: Request, *, streaming: bool) -> Generator[Event, None, Response]:
    request_id = uuid.uuid4().hex
    cfg = layer_config.current()
    ranked, rejected = engine.route(request, cfg)
    record = TraceRecord(request_id=request_id, task_class=request.task_class, affinity_key=request.affinity_key,
                         data_class=request.data_class, ranked=[c.endpoint.id for c in ranked],
                         rejected=list(rejected), request=request)
    call_started = clock()
    if not ranked:
        err = engine.nothing_eligible(request, rejected, engine.catalog(cfg))
        record.outcome = err.type
        _publish(record)
        raise err

    queue = list(ranked)
    first = queue[0]
    failed_upstreams: set[str] = set()
    report = adapt.Report()
    progress = _Progress()
    last_error: ModelError | None = None
    previous: Candidate | None = None

    while queue:
        candidate = next((queue.pop(i) for i, c in enumerate(queue) if _upstream(c) not in failed_upstreams),
                         None) or queue.pop(0)
        if previous is not None and not request.requirements.allow_family_change and not _same_family(first, candidate):
            report.warnings.append(f"{candidate.endpoint.id} was skipped: it's a different model family, and this "
                                   "request doesn't allow switching families.")
            continue
        if previous is not None:
            record.fallbacks.append(Fallback(previous.endpoint.id, candidate.endpoint.id,
                                             str(last_error) if last_error else "the previous endpoint failed"))
        waits = cfg.settings.retry_waits_s or (1.0,)
        for attempt_no in range(cfg.settings.retries + 1):
            attempt_started = clock()
            progress.first_at = None
            try:
                attempt_report = adapt.Report(dict(report.features), list(report.warnings))
                assembled, usage, data = yield from _one(request, candidate, cfg, attempt_report, progress, record,
                                                         streaming=streaming)
            except ModelError as err:
                err.at(candidate.endpoint.id)
                last_error = err
                record.attempts.append(Attempt(candidate.endpoint.id, err.type, str(err),
                                               int((clock() - attempt_started) * 1000)))
                _note_failure(candidate, err, cfg)
                if progress.emitted or not err.retryable:
                    record.outcome = err.type
                    record.total_ms = int((clock() - call_started) * 1000)
                    record.endpoint_id = candidate.endpoint.id
                    _publish(record)
                    raise
                wait = err.retry_after if err.retry_after is not None else waits[min(attempt_no, len(waits) - 1)]
                if attempt_no >= cfg.settings.retries or wait > LONG_WAIT_S:
                    break
                sleep(wait)
                continue

            # It answered.
            report = attempt_report
            elapsed = int((clock() - attempt_started) * 1000)
            ttft = int((progress.first_at - attempt_started) * 1000) if progress.first_at else None
            record.attempts.append(Attempt(candidate.endpoint.id, None, None, elapsed))
            state.record_success(candidate.endpoint.id)
            state.record_latency(candidate.endpoint.id, ttft, elapsed)
            if usage.cost:
                state.add_spend(usage.cost)
            engine.router.remember(request.affinity_key, candidate.endpoint.id)
            fin = assembled.finish
            response = Response(
                items=tuple(assembled.items), stop_reason=fin.stop_reason if fin else "stop", usage=usage,
                provenance=Provenance(candidate.endpoint.id, fin.reported_model if fin else None,
                                      tuple(record.attempts), tuple(record.fallbacks)),
                report=FeatureReport(dict(report.features), tuple(report.warnings)),
                data=data, request_id=request_id)
            record.endpoint_id = candidate.endpoint.id
            record.reported_model = response.provenance.reported_model
            record.ttft_ms, record.total_ms = ttft, int((clock() - call_started) * 1000)
            record.usage, record.report, record.response = usage, response.report, response
            if record.fallbacks:
                record.signals["fallbacks"] = len(record.fallbacks)
            _publish(record)
            return response

        failed_upstreams.add(_upstream(candidate))
        previous = candidate

    record.outcome = last_error.type if last_error else "no_eligible_endpoint"
    record.total_ms = int((clock() - call_started) * 1000)
    _publish(record)
    if last_error is not None:
        raise last_error
    raise engine.nothing_eligible(request, rejected, engine.catalog(cfg))


def generate(request: Request) -> Response:
    gen = run(request, streaming=False)
    while True:
        try:
            next(gen)
        except StopIteration as done:
            return done.value


def stream(request: Request) -> Iterator[Event]:
    """Canonical events, then `Done(response)`; or an `ErrorEvent`, then nothing."""
    gen = run(request, streaming=True)
    while True:
        try:
            event = next(gen)
        except StopIteration as done:
            yield Done(done.value)
            return
        except ModelError as err:
            yield ErrorEvent(err)
            return
        yield event


def _install_tracing() -> None:
    from . import trace

    if trace.write not in observers:
        observers.append(trace.write)


_install_tracing()
