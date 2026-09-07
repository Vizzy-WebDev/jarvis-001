"""One prompt, one answer — the narrow seam a TOOL is allowed to ask through.

A third way to drive a model, alongside a turn (the orchestrator) and a research
lookup. What makes it its own module rather than a call into the gateway is the
boundary it preserves: nothing under `jarvis/tools/` may import the gateway, and
`tests/test_architecture.py` asserts that. A tool should be able to say "answer
this", not to pick a candidate and drive a stream — the second is the provider
layer's job, and a tool reaching into it is how a second, subtly different
model-calling path gets written.

Safe for a tool to import: no path from here to the tool loader, the executor or
the orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Reply:
    ok: bool
    text: str = ""
    data: Any = None
    model_id: str | None = None
    error: str | None = None


def ask_model(prompt: str, *, system: str = "", want_json: bool = False,
              media: list[dict[str, Any]] | None = None,
              need: dict[str, bool] | None = None,
              model_id: str | None = None, only: bool = False,
              background: bool = True) -> Reply:
    """Ask once. Returns a Reply rather than raising.

    A tool's caller is a model mid-turn, and "no model was available" is
    something to say plainly in a tool result — not an exception that turns into
    a failed turn with no explanation.
    """
    from .gateway.client import ask
    from .gateway.routing import Task

    task = Task(text=prompt, needs_tools=False, background=background,
                need=dict(need or {}))
    try:
        answer = ask(prompt, system=system, want_json=want_json, task=task,
                     media=media, model_id=model_id, only=only)
    except Exception as err:  # noqa: BLE001 — every provider failure reads the same here
        return Reply(ok=False, error=str(err))
    return Reply(ok=True, text=answer.text, data=answer.data, model_id=answer.model_id)
