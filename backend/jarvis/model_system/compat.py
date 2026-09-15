"""The one-off "ask a model something" seam — the third way of driving a
model, alongside a full conversational turn (`Gateway.stream()`, used by the
orchestrator) and a direct pinned call (`gateway.execute()`).

Every subsystem outside `jarvis/tools/` that just wants an answer to one
question — memory extraction, verification, heartbeat triage, improvement
synthesis, scheduled briefings, research, the desktop-control planner — goes
through `ask()` here rather than building its own `AIRequest` by hand. It
walks the SAME ranked candidate list as a full turn (`ai/router.py`), which
is the whole point: a one-off call that bypassed routing used to be a second,
subtly different model-calling path that never marked a failing model
unhealthy, so it kept retrying models the chat loop had already benched.

**A model that answers but not in the requested JSON shape is not marked
unhealthy.** It is working, it is just not following a format instruction —
worth keeping distinct, because benching a healthy model for that would
gradually empty the roster on exactly the weak models most likely to do it.
The next candidate is tried instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from ..events.bus import EventBus
from ..events import bus as default_bus
from . import health
from .errors import benches_the_model, classify
from .fallback import NoModelAvailable, publish_call_completed
from .gateway import execute_on
from .jsonish import extract_json
from .profiles import resolve_request
from .request import (
    AIRequest, Attachment, Completed, Message, Modality, Preferences, Requirements, Role, TextDelta,
)
from .router import rank
from . import usage as usage_store
from .gateway import new_request_id

#: How many candidates one call will pay a full round trip for. Same bound
#: `ai/fallback.py` uses, for the same reason: a stale roster must not turn
#: one question into a minutes-long walk through known failures.
MAX_ATTEMPTS = 4


@dataclass(frozen=True)
class Task:
    """What a one-off ask needs, in routing terms — kept for callers that
    already build one (memory review, improvement synthesis, ...) rather
    than passing `role`/`need` as loose keyword arguments."""

    text: str = ""
    role: Role = Role.UTILITY
    needs_tools: bool = False
    estimated_tokens: int = 0
    need: dict[str, bool] = field(default_factory=dict)


@dataclass
class Answer:
    """One prompt, one answer, no tools."""

    text: str
    model_id: str | None = None
    data: Any = None
    tried: list[str] = field(default_factory=list)


def _attachment_from_media(item: dict[str, Any]) -> Attachment:
    kind = item.get("kind") or "image"
    modality = {"image": Modality.IMAGE, "video": Modality.VIDEO,
                "audio": Modality.AUDIO, "file": Modality.FILE}.get(kind, Modality.IMAGE)
    import base64
    data = base64.b64decode(item["dataBase64"]) if item.get("dataBase64") else None
    return Attachment(modality=modality, mime_type=item.get("mimeType") or "image/png",
                      data=data, uri=item.get("uri"))


def ask(
    prompt: str,
    *,
    system: str = "",
    want_json: bool = False,
    task: Task | None = None,
    model_id: str | None = None,
    media: list[dict[str, Any]] | None = None,
    only: bool = False,
    event_bus: EventBus | None = None,
) -> Answer:
    """A single question with no tools and no transcript.

    `only` disables fallback entirely, and is REQUIRED whenever `media` was
    uploaded against one specific model's own API — the next candidate would
    be handed a file URI it has no right to read, which fails in a way that
    looks like the file being bad rather than like a routing mistake.
    """
    bus = event_bus or default_bus
    task = task or Task(text=prompt, role=Role.UTILITY)
    requirements = Requirements(needs_tools=task.needs_tools, capabilities=dict(task.need or {}),
                                estimated_input_tokens=task.estimated_tokens)
    preferences = Preferences(role=task.role, preferred_model_id=model_id)

    attachments = tuple(_attachment_from_media(m) for m in (media or []) if isinstance(m, dict))
    message = Message(role="user", text=prompt, attachments=attachments)
    base_request = resolve_request(AIRequest(
        messages=(message,), system=system, requirements=requirements, preferences=preferences,
        model_id=model_id,
    ))

    candidates = rank(base_request.requirements, base_request.preferences)
    attempts = candidates[:1] if only else candidates[:MAX_ATTEMPTS]
    if not attempts:
        raise NoModelAvailable("There are no models set up yet, so I can't answer that.")

    request_id = new_request_id()
    tried: list[str] = []
    errors: list[tuple[str, str]] = []

    for model in attempts:
        tried.append(model.id)
        per_model = replace(base_request, model_id=model.id)
        text_parts: list[str] = []
        completed: Completed | None = None
        try:
            for event in execute_on(model, per_model):
                if isinstance(event, TextDelta):
                    text_parts.append(event.text)
                elif isinstance(event, Completed):
                    completed = event
        except Exception as err:  # noqa: BLE001
            kind = classify(err)
            if benches_the_model(kind):
                health.record_failure(model.id, kind, detail=str(err), technical=repr(err))
            usage_store.record(request_id=request_id, model_id=model.id,
                               provider_id=model.provider.id, role=task.role.value,
                               success=False, error_type=kind.value, background=task.role.background_work)
            errors.append((model.id, str(err)))
            continue

        text = (completed.text if completed and completed.text else "".join(text_parts))
        health.record_success(model.id)
        publish_call_completed(model, per_model, completed.usage if completed else None, bus)
        usage_store.record(request_id=request_id, model_id=model.id, provider_id=model.provider.id,
                           role=task.role.value, success=True,
                           usage=completed.usage if completed else None,
                           background=task.role.background_work)

        if not want_json:
            return Answer(text=text, model_id=model.id, tried=tried)

        data = extract_json(text)
        if data is None:
            # Deliberately no health record — see this module's docstring.
            errors.append((model.id, "did not answer in the requested format"))
            continue
        return Answer(text=text, model_id=model.id, data=data, tried=tried)

    if not errors:
        raise NoModelAvailable("I couldn't get an answer from any model.")
    first = errors[0][1]
    raise NoModelAvailable(
        f"I tried {len(errors)} model{'s' if len(errors) != 1 else ''} without success. "
        f"The first said: {first}",
        {"requestId": request_id, "tried": [{"modelId": m, "error": e} for m, e in errors]},
    )
