"""One prompt, one answer — what `jarvis.ai.ask` runs on.

Kept apart from `client.py` because tools reach this (through `ai.py`) and must
not be led, by way of it, into the orchestrator.

Each caller states its task class and how sensitive its data is; the person's
selection, if they named one, is the pin, exactly as in a chat turn. A JSON answer
is asked for as structured output (best effort: enforced where the model can,
emulated, validated and repaired where it can't).
"""

from __future__ import annotations

from typing import Any

from ..ai import Answer
from . import boundary, config, generate, settings
from .errors import ModelError
from .types import Hints, ImagePart, Message, OutputSpec, Request, Requirements, Section, TextPart

#: What "reply with JSON" means when the caller gives no schema of its own.
ANY_OBJECT = {"type": "object"}


def ask(prompt: str, *, data_class: str, task_class: str, system: str = "", want_json: bool = False,
        schema: dict[str, Any] | None = None, media: list[dict[str, Any]] | None = None,
        need: dict[str, bool] | None = None, background: bool = False) -> Answer:
    parts: list[Any] = [TextPart(prompt)]
    for item in media or []:
        if item.get("dataBase64"):
            parts.append(ImagePart(str(item.get("mimeType") or "image/png"), item["dataBase64"]))
    try:
        pin = None if settings.is_auto() else settings.SELECTED
    except config.ConfigError:
        pin = None
    request = Request(
        task_class=task_class, data_class=data_class,  # type: ignore[arg-type]
        items=(Message("user", tuple(parts)),),
        instructions=(Section("instructions", system),) if system else (),
        output=OutputSpec("json", schema or ANY_OBJECT, "best_effort") if (want_json or schema) else OutputSpec(),
        requirements=Requirements(capabilities=frozenset({"image_in"} if (need or {}).get("vision") else ()),
                                  pin=pin),
        hints=Hints(reasoning_effort=settings.effort() if pin else None))
    try:
        response = generate(request)
    except ModelError as err:
        raise boundary.failure(err, pinned_selection=pin is not None) from err
    # A one-shot answer is never streamed to anyone: the cost ledger counts it as background work,
    # as it always has. `background` is accepted for callers that say so explicitly.
    boundary.publish_completed(response, session_id=None, background=True)
    return Answer(text=response.text, data=response.data, model_id=boundary.reported_model(response))
