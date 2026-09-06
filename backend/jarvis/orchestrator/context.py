"""The context seam (§23) — what the model is actually given for this turn.

§23 asks for relevance-based assembly: recent conversation, relevant long-term
memory, the current task, tool results, and a stated token budget — not "dump
everything into the prompt". The Node implementation injects the ENTIRE approved
memory set into every single call, and builds its system prompt by pulling from
six live stores at call time.

The real assembler is a later step of this build and needs memory ported first.
What matters now is that the orchestrator asks a seam for its context instead of
assembling it inline, so the assembler can be swapped in without touching the
turn loop — and so what the model gets is inspectable in one place.

`WindowContext` below is honest about being the simple version: the recent
conversation window and a caller-supplied system prompt, with no relevance
selection at all. It does NOT pretend to rank anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .. import conversation


@dataclass(frozen=True)
class AssembledContext:
    system: str
    messages: list[dict[str, Any]]
    #: What went in and why — for observability (§25) and for the user asking
    #: "why did you think that". Empty until the real assembler lands.
    included: tuple[str, ...] = ()
    notes: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ContextAssembler(Protocol):
    def assemble(self, *, session_id: str, text: str) -> AssembledContext:
        ...


class WindowContext:
    """The recent conversation window, plus a fixed system prompt.

    No relevance selection, no memory, no budget — deliberately, and said out
    loud rather than implied by a name like `SmartContext`.
    """

    def __init__(self, system: str = "") -> None:
        self._system = system

    def assemble(self, *, session_id: str, text: str) -> AssembledContext:
        messages = conversation.get_messages(session_id)
        return AssembledContext(
            system=self._system,
            messages=messages,
            included=("conversation_window",),
            notes={"strategy": "recent window only, no relevance selection"},
        )
