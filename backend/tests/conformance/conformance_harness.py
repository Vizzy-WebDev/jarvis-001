"""What every provider module must do, checked the same way for each (Priority 10).

Each adapter's own conformance file supplies its canonical raw error payloads and
calls these. Three things are checked for every adapter:

1. **Its shape** — it satisfies `models.adapter.ProviderAdapter`, with the signatures
   the rest of the system calls it by.
2. **Its errors** — every canonical failure its provider really sends (status, body,
   headers) normalizes to the expected kind / scope / retryability / wait, and the same
   payload sent over a real socket produces the same verdict from `stream`.
3. **Its stream** — a minimal request through a real stub server yields text deltas and
   then exactly one `Finished`, whose text is those deltas; a tool call comes back as a
   `ToolUse` with parsed arguments.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from types import ModuleType
from typing import Any

import pytest

from jarvis import conversation
from jarvis.models.adapter import ProviderAdapter
from jarvis.models.errors import SCOPES, ProviderError
from jarvis.models.types import Finished, RawError, Target, TextDelta, ToolUse, Usage


@dataclass(frozen=True)
class Case:
    """One canonical failure, as the provider really sends it, and what it must mean."""

    name: str
    status: int | None
    body: Any
    kind: str
    scope: str
    retryable: bool
    headers: dict[str, str] | None = None
    retry_after_s: float | None = None


def raw_of(case: Case) -> RawError:
    from jarvis.models.providers import _wire

    return RawError(status=case.status, headers={k.lower(): v for k, v in (case.headers or {}).items()},
                    body=case.body if not isinstance(case.body, str) else None,
                    words=_wire.words_of(case.body if not isinstance(case.body, str) else None,
                                         case.body if isinstance(case.body, str) else ""),
                    url="https://provider.example/v1")


def check_shape(module: ModuleType) -> None:
    assert isinstance(module, ProviderAdapter), f"{module.__name__} does not satisfy ProviderAdapter"
    assert isinstance(module.FORMAT, str) and module.FORMAT
    stream = inspect.signature(module.stream).parameters
    assert list(stream)[:2] == ["target", "request"], "stream(target, request, *, facts)"
    assert stream["facts"].kind is inspect.Parameter.KEYWORD_ONLY
    assert list(inspect.signature(module.normalize_error).parameters) == ["raw"]
    assert list(inspect.signature(module.check).parameters) == ["target"]
    assert list(inspect.signature(module.discover).parameters) == ["target"]


def check_case(module: ModuleType, case: Case) -> ProviderError:
    err = module.normalize_error(raw_of(case))
    assert isinstance(err, ProviderError), case.name
    assert err.scope in SCOPES, case.name
    got = (err.kind, err.scope, err.retryable_elsewhere, err.retry_after_s)
    want = (case.kind, case.scope, case.retryable, case.retry_after_s)
    assert got == want, f"{case.name}: got {got}, want {want}"
    assert str(err).strip(), f"{case.name}: a failure must be said in words"
    return err


def minimal_request(model_id: str = "stub-model-a", tools: list[dict[str, Any]] | None = None):
    return conversation.to_chat_request([{"role": "user", "text": "hello"}], system="be brief",
                                        tools=tools, model_id=model_id)


def check_minimal_stream(module: ModuleType, stub) -> None:
    events = list(module.stream(Target(stub.base_url, "k"), minimal_request()))
    assert events, "a stream must yield something"
    *deltas, last = events
    assert isinstance(last, Finished), "a stream ends with exactly one Finished"
    assert all(isinstance(e, TextDelta) for e in deltas), "only text comes before the Finished"
    assert sum(isinstance(e, Finished) for e in events) == 1
    assert last.text == "".join(d.text for d in deltas) == stub.reply
    assert last.finish_reason == "stop" and last.tool_calls == ()
    assert last.usage is None or isinstance(last.usage, Usage)
    assert last.model_id == "stub-model-a"


def check_tool_stream(module: ModuleType, stub) -> None:
    tools = [{"name": "get_time", "description": "time", "parameters": {"type": "object",
                                                                         "properties": {"zone": {"type": "string"}}}}]
    events = list(module.stream(Target(stub.base_url, "k"), minimal_request(tools=tools)))
    finished = events[-1]
    assert isinstance(finished, Finished) and finished.finish_reason == "tool_calls"
    assert finished.tool_calls == (ToolUse(id=finished.tool_calls[0].id, name="get_time", args={"zone": "UTC"}),)


def check_over_the_wire(module: ModuleType, stub, case: Case) -> None:
    """The same canonical failure, sent by a real server, reaches the same verdict —
    so the headers and the body really do flow from the socket into `normalize_error`."""
    stub.fail_with = (case.status, case.body, case.headers or {})
    with pytest.raises(ProviderError) as caught:
        list(module.stream(Target(stub.base_url, "k"), minimal_request()))
    expected = module.normalize_error(raw_of(case))
    got = caught.value
    assert (got.kind, got.scope, got.retryable_elsewhere, got.retry_after_s, got.status) == (
        expected.kind, expected.scope, expected.retryable_elsewhere, expected.retry_after_s, case.status)
