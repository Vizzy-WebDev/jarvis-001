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
from . import providers, runtime, selection
from .errors import ProviderError
from .types import Finished

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
    """Ask the selected model once.

    `only` is accepted and unused: it meant "do not fall back", and nothing here
    ever falls back. Raises `NoModelAvailable` — the same plain error the turn
    loop uses — when the selection cannot be run or the provider refuses.
    """
    resolved = selection.resolve(model_id)
    selection.check_needs(need)
    provider = providers.for_format(resolved.connection.format)

    message: dict[str, Any] = {"role": "user", "text": prompt}
    if media:
        message["media"] = media
    finished: Finished | None = None
    try:
        for event in provider.stream(
            resolved.target, model_id=resolved.model.model_id, messages=[message],
            system=(system or "") + (_JSON_NOTE if want_json else ""), tools=[],
            effort=resolved.effort, facts=resolved.model.facts,
        ):
            if isinstance(event, Finished):
                finished = event
    except ProviderError as err:
        raise runtime.failure(resolved, err) from err
    if finished is None:
        raise runtime.failure(resolved, ProviderError("The reply stopped part-way.", kind="reply"))

    runtime.publish_completed(
        resolved, session_id=None, reported=finished.model_id, usage=finished.usage,
        background=background or role in runtime.BACKGROUND_ROLES,
    )
    return Answer(text=finished.text, data=parse_json(finished.text) if want_json else None,
                  model_id=finished.model_id or resolved.model.model_id)
