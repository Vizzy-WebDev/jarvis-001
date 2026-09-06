"""The orchestrator (§9) — the one pipeline every request travels.

§9: "Requests should flow through: input normalization -> context assembly ->
model reasoning -> tool selection -> permission check -> execution -> result
observation -> response generation. Avoid scattering this logic."

It composes the pieces rather than containing them: routing lives in `intent`,
authorization in `policy`, execution in `capabilities.execute`, observability on
`events`. What lives here is the loop, the lifecycle, and the decision about when
a model is needed at all.
"""

from .context import AssembledContext, ContextAssembler, WindowContext
from .model_port import ModelClient, StepComplete, TextChunk, ToolCall
from .pipeline import (
    ApprovalRequired,
    Chunk,
    Done,
    Failed,
    Interrupted,
    MAX_STEPS,
    Orchestrator,
    Routed,
    Switched,
    ToolRan,
    TurnEvent,
    TurnRequest,
)

__all__ = [
    "AssembledContext",
    "ApprovalRequired",
    "Chunk",
    "ContextAssembler",
    "Done",
    "Failed",
    "Interrupted",
    "MAX_STEPS",
    "ModelClient",
    "Orchestrator",
    "Routed",
    "StepComplete",
    "Switched",
    "TextChunk",
    "ToolCall",
    "ToolRan",
    "TurnEvent",
    "TurnRequest",
    "WindowContext",
]
