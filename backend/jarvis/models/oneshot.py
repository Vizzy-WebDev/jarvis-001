"""One prompt, one answer — what `jarvis.ai.ask` runs on.

Kept apart from `client.py` because tools reach this (through `ai.py`) and must
not be led, by way of it, into the orchestrator. It imports the providers and the
selection and nothing that imports the turn loop.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..ai import Answer
from . import attempt, runtime, selection

_JSON_NOTE = ("\n\nReply with a single JSON object and nothing else — no commentary and no code fence.")
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_json(text: str) -> Any:
    """The JSON object in a reply, or `None`. Tolerant of a code fence or a line of
    preamble, because models add them; never invents anything that isn't there."""
    stripped = _FENCE.sub("", (text or "").strip())
    for candidate in (stripped, stripped[stripped.find("{"):stripped.rfind("}") + 1] if "{" in stripped else ""):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    return None


def ask(prompt: str, *, system: str = "", want_json: bool = False,
        media: list[dict[str, Any]] | None = None, need: dict[str, bool] | None = None,
        model_id: str | None = None, only: bool = False, role: str = "utility",
        background: bool = False) -> Answer:
    """Ask the selected model once — or, under Auto, whichever model Auto picks.

    `only` is accepted and unused: it meant "do not fall back", and a named model is
    never replaced (Auto, which the person chose, is the only thing that moves on).
    Raises `NoModelAvailable` — the same plain error the turn
    loop uses — when the selection cannot be run or the provider refuses.
    """
    message: dict[str, Any] = {"role": "user", "text": prompt}
    if media:
        message["media"] = media
    plan = selection.plan(model_id, needs_images=any(m.get("kind", "image") == "image" for m in media or []))
    selection.check_needs(need)

    run = attempt.run(plan, messages=[message], system=(system or "") + (_JSON_NOTE if want_json else ""),
                      tools=[], named=model_id is None)
    while True:  # nobody is watching this one speak; only how it ended matters
        try:
            next(run)
        except StopIteration as done:
            result = done.value
            break
    resolved, finished = result.resolved, result.finished

    runtime.publish_completed(
        resolved, session_id=None, reported=finished.model_id, usage=finished.usage,
        background=background or role in runtime.BACKGROUND_ROLES,
    )
    return Answer(text=finished.text, data=parse_json(finished.text) if want_json else None,
                  model_id=finished.model_id or resolved.model.model_id)
