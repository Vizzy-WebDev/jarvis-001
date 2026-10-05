"""The whole plug-in surface for something worth watching.

Pure and zero-import on purpose: the engine that ticks and the layer that judges
urgency know nothing about what any source actually watches, and a new one is a
single `register()` call rather than a change to either.

A source is:

* `id` — stable; it becomes the schedule row's key and the trace's ref.
* `default_interval_ms` — how often its items are worth checking.
* `list_items()` — what it is currently watching, as `{"itemKey", "intervalMs"?}`.
* `check(item_key)` — `{"finding": {...} | None, "checkState": ... }`.

**A source whose condition can stay true across many ticks must keep its own
"already reported" memory in `checkState`.** A tier-3 verdict creates no outbox
row, so the broker's own duplicate check has nothing to see, and the same
unresolved situation will otherwise produce a fresh finding, a fresh model call
and a fresh notification on every single tick, forever. That is not a
hypothetical: it is the bug this contract exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class Source:
    id: str
    default_interval_ms: int
    list_items: Callable[[], list[dict[str, Any]]]
    check: Callable[[str], dict[str, Any]]


_sources: dict[str, Source] = {}


def register(source: Source) -> None:
    if not getattr(source, "id", None):
        raise ValueError("a heartbeat source needs an id")
    if not callable(source.list_items) or not callable(source.check):
        raise TypeError(f'source "{source.id}" must implement list_items() and check()')
    _sources[source.id] = source


def list_sources() -> list[Source]:
    return list(_sources.values())


def get_source(source_id: str) -> Source | None:
    return _sources.get(source_id)


def reset() -> None:
    _sources.clear()
