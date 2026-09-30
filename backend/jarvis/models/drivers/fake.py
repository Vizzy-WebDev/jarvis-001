"""A scripted driver for tests. It speaks no wire at all: each connection using it
plays back whatever a test queued for it, and records every call it was given.

    fake.reset()
    fake.queue("local-a", fake.reply("hello"))              # one step: a text answer
    fake.queue("local-a", errors.Unavailable("down"))      # the next call fails
    fake.listing("local-a", [Discovered("m1")])            # what discovery returns

A queued step is a list of driver events, a `ModelError` to raise before anything
is said, or a callable `(prepared) -> iterable` for anything else (a failure part
way through a stream, a tool call that needs a freshly minted id). With nothing
queued a call answers "ok".
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Iterator

from ..errors import ModelError, Unavailable
from ..prepared import ConnInfo, Discovered, Prepared, Unexpressible
from ..types import (Finish, SealedEvent, TextDelta, ToolArgsDelta, ToolCall, ToolCallCompleted,
                     ToolCallStarted, Usage)

NAME = "fake"


class _AnyFlag(frozenset):
    """A test driver understands whatever wire flag a test gives it."""

    def __contains__(self, item: object) -> bool:
        return True


QUIRKS = _AnyFlag()

DEFAULT_CAPABILITIES: dict[str, Any] = {"text_in": True, "streaming": True}

_lock = threading.Lock()
_queues: dict[str, list[Any]] = {}
_listings: dict[str, Any] = {}
_embeddings: dict[str, Callable[[str, list[str]], list[list[float]]]] = {}


@dataclass(frozen=True)
class Call:
    connection: str
    prepared: Prepared


CALLS: list[Call] = []


def reset() -> None:
    with _lock:
        _queues.clear()
        _listings.clear()
        _embeddings.clear()
        CALLS.clear()


def queue(connection: str, *steps: Any) -> None:
    with _lock:
        _queues.setdefault(connection, []).extend(steps)


def listing(connection: str, models: list[Discovered] | ModelError) -> None:
    with _lock:
        _listings[connection] = models


def embedder(connection: str, fn: Callable[[str, list[str]], list[list[float]]]) -> None:
    with _lock:
        _embeddings[connection] = fn


def calls(connection: str | None = None) -> list[Call]:
    return [c for c in CALLS if connection is None or c.connection == connection]


# --- step builders -----------------------------------------------------------------------

def reply(text: str, *, usage: Usage | None = None, model: str | None = None,
          sealed: list[tuple[str, Any]] | None = None, chunks: int = 1) -> list[Any]:
    events: list[Any] = [SealedEvent(kind, payload) for kind, payload in sealed or []]
    size = max(1, len(text) // max(chunks, 1)) if text else 1
    events += [TextDelta(text[i:i + size]) for i in range(0, len(text), size)]
    events.append(Finish("stop", usage or Usage(input=10, output=5), model))
    return events


def tools(*calls_: tuple[str, str], text: str = "", native_ids: bool = False,
          sealed: list[tuple[str, Any]] | None = None, usage: Usage | None = None) -> Callable[[Prepared], Iterable[Any]]:
    """Tool calls as `(name, arguments-as-JSON-text)`. `native_ids` makes it behave
    like a server with its own ids: they are carried as a Sealed "ids" item."""
    import json

    def run(prepared: Prepared) -> Iterator[Any]:
        for kind, payload in sealed or []:
            yield SealedEvent(kind, payload)
        if text:
            yield TextDelta(text)
        mapping: dict[str, str] = {}
        for index, (name, raw) in enumerate(calls_):
            cid = prepared.mint_id()
            if native_ids:
                mapping[cid] = f"native_{index}"
            yield ToolCallStarted(cid, name)
            yield ToolArgsDelta(cid, raw)
            try:
                args = json.loads(raw) if raw.strip() else {}
                parsed = args if isinstance(args, dict) else None
            except ValueError:
                parsed = None
            yield ToolCallCompleted(ToolCall(cid, name, parsed, None if parsed is not None else raw))
        if mapping:
            yield SealedEvent("ids", mapping)
        yield Finish("tool_calls", usage or Usage(input=10, output=5))
    return run


def broken_after(text: str, error: ModelError) -> Callable[[Prepared], Iterable[Any]]:
    """Says `text`, then fails part way through the stream."""
    def run(prepared: Prepared) -> Iterator[Any]:
        yield TextDelta(text)
        raise error
    return run


# --- the driver interface ------------------------------------------------------------------

def stream(conn: ConnInfo, prepared: Prepared) -> Iterator[Any]:
    with _lock:
        CALLS.append(Call(conn.name, prepared))
        pending = _queues.get(conn.name) or []
        step = pending.pop(0) if pending else None
    if step is None:
        step = reply("ok")
    if isinstance(step, ModelError):
        raise step
    events = step(prepared) if callable(step) else step
    yield from events


def discover(conn: ConnInfo) -> list[Discovered]:
    with _lock:
        found = _listings.get(conn.name)
    if isinstance(found, ModelError):
        raise found
    if found is None:
        raise Unavailable(f"{conn.name} didn't answer.")
    return list(found)


def translate_schema(schema: Any, quirks: Any = None) -> Any:
    """Identity — except a schema a test marks as inexpressible here."""
    if isinstance(schema, dict):
        if schema.get("x-fake-inexpressible"):
            raise Unexpressible("this fake server can't express that schema")
        return {k: translate_schema(v, quirks) for k, v in schema.items()}
    if isinstance(schema, list):
        return [translate_schema(v, quirks) for v in schema]
    return schema


def embed(conn: ConnInfo, model_id: str, inputs: list[str]) -> list[list[float]]:
    with _lock:
        fn = _embeddings.get(conn.name)
        pending = _queues.get(conn.name) or []
        step = pending.pop(0) if pending and isinstance(pending[0], ModelError) else None
    if step is not None:
        raise step
    if fn is None:
        return [[float(len(text)), 0.0] for text in inputs]
    return fn(model_id, inputs)
