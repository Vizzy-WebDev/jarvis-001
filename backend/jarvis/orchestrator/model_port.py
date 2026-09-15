"""What the orchestrator needs from the AI Model System, and nothing more.

The turn loop must not know which provider answered, how streaming works on
that provider's wire, or how a tool call is spelled in its JSON — a narrow
port is what lets `jarvis/model_system/` be replaced, stubbed, or extended
without the orchestrator noticing, and it is what let the desktop control
loop (`control/session.py`) reuse the exact same seam for a different
perceive step instead of reimplementing model-calling from scratch.

This is now a thin seam directly onto `jarvis/model_system`'s own normalized
vocabulary — `TextDelta`/`Completed`/`ModelSwitched`/`ToolCall` ARE the port,
re-exported here rather than wrapped in a second, orchestrator-owned copy of
the same shapes. `ModelUnavailable` is `ai/fallback.py`'s own
`NoModelAvailable`, for the same reason: a caller catching it needs the real
`.detail` the routing trace was built from, not a summary of it.
"""

from __future__ import annotations

from typing import Any, Iterator, Protocol, runtime_checkable

from ..model_system.fallback import NoModelAvailable as ModelUnavailable
from ..model_system.request import (
    Completed as StepComplete,
    ErrorEvent,
    ModelSwitched,
    TextDelta as TextChunk,
    ToolCall,
)

#: What a model client yields, in order: any number of chunks (possibly
#: interrupted by a switch), then exactly one completed step.
ModelEvent = TextChunk | StepComplete | ModelSwitched | ErrorEvent


@runtime_checkable
class ModelClient(Protocol):
    """A provider-agnostic single step of generation."""

    def stream(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]],
        session_id: str,
        model_id: str | None = None,
        role: str | None = None,
        need: dict[str, bool] | None = None,
    ) -> Iterator[ModelEvent]:
        """`need` is what this turn REQUIRES — vision, video, audio, web
        search. Part of the port because it is the orchestrator's own
        knowledge: it is the side that knows an image was attached. A model
        that cannot see one must be excluded BEFORE it is called, not
        discovered to be blind by being handed bytes it cannot read.

        `role` is the same kind of knowledge: the orchestrator knows whether
        this turn was spoken, scheduled or typed, and the model system is the
        side that knows a spoken turn should be ranked for latency and a
        scheduled one for price. A plain string, not `model_system.request`'s
        own `Role` enum — the orchestrator may hold no opinion about the
        model system's internal vocabulary beyond this port, only about the
        shared strings the two sides have agreed mean the same thing.

        An unrecognised role is not an error: `model_system.request.role_from()`
        treats it as an ordinary conversation, so a caller that has not
        classified its turn gets sensible routing rather than a failure.
        """
        ...
