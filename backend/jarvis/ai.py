"""One prompt, one answer — the narrow seam anything outside the turn loop uses to
ask a model something.

An ask runs on whichever model the person has selected, or under Auto on what the
model layer routes to (`jarvis/models/`). When that can't be done — nothing selected, the connection gone, the provider
refusing — it ends in `NoModelAvailable` with the reason in it, or in a `Reply`
with `ok=False` for callers that want a value rather than an exception. Each
caller already has a plain "no model" branch.

A leaf on purpose — nothing under `jarvis/tools/` may import the orchestrator,
and this module imports nothing from it — so a tool can safely import it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

NO_MODEL_MESSAGE = "Jarvis has no AI model connected right now, so it can't answer yet."


class NoModelAvailable(RuntimeError):
    """Nothing could serve this request. `detail` carries the reasons, when
    there are any."""

    def __init__(self, message: str = NO_MODEL_MESSAGE,
                 detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.detail = detail or {}


@dataclass(frozen=True)
class Answer:
    text: str = ""
    data: Any = None
    model_id: str | None = None


@dataclass(frozen=True)
class Reply:
    ok: bool
    text: str = ""
    data: Any = None
    model_id: str | None = None
    error: str | None = None


def ask(prompt: str, *, data_class: str, task_class: str, system: str = "", want_json: bool = False,
        schema: dict[str, Any] | None = None, media: list[dict[str, Any]] | None = None,
        need: dict[str, bool] | None = None, background: bool = False) -> Answer:
    """Ask once, on the person's selected model — or, under Auto, whichever model the
    task class's route picks.

    `data_class` ('public' | 'personal' | 'sensitive') says how sensitive what is
    sent is, and `task_class` what kind of work it is. Both are required: the model
    layer routes on them and records them.

    Raises `NoModelAvailable` — with the reason in its message — when nothing can
    answer. It never asks a different model than the person selected.

    Imported here rather than at the top so this module stays a leaf that tools can
    import: the one-shot path reaches the model layer, and nothing that reaches
    back into the turn loop.
    """
    from .models.oneshot import ask as _ask

    return _ask(prompt, data_class=data_class, task_class=task_class, system=system, want_json=want_json,
                schema=schema, media=media, need=need, background=background)


def ask_model(prompt: str, *, data_class: str, task_class: str, system: str = "", want_json: bool = False,
              schema: dict[str, Any] | None = None, media: list[dict[str, Any]] | None = None,
              need: dict[str, bool] | None = None, background: bool = False) -> Reply:
    """Ask once. Returns a Reply rather than raising.

    A tool's caller is a model mid-turn, and "no model was available" is
    something to say plainly in a tool result — not an exception that turns into
    a failed turn with no explanation.
    """
    try:
        answer = ask(prompt, data_class=data_class, task_class=task_class, system=system, want_json=want_json,
                     schema=schema, media=media, need=need, background=background)
    except Exception as err:  # noqa: BLE001 — every failure reads the same here
        return Reply(ok=False, error=str(err))
    return Reply(ok=True, text=answer.text, data=answer.data, model_id=answer.model_id)
