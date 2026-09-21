"""One prompt, one answer — the narrow seam anything outside the turn loop uses to
ask a model something.

There is no AI model system in Jarvis right now, so every ask ends the same way:
`NoModelAvailable`, or a `Reply` with `ok=False` for callers that want a value
rather than an exception. Each caller already has a plain "no model" branch.

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


def ask(prompt: str, *, system: str = "", want_json: bool = False,
        media: list[dict[str, Any]] | None = None,
        need: dict[str, bool] | None = None,
        model_id: str | None = None, only: bool = False) -> Answer:
    """Ask once. Always raises `NoModelAvailable` until a model system exists."""
    raise NoModelAvailable()


def ask_model(prompt: str, *, system: str = "", want_json: bool = False,
              media: list[dict[str, Any]] | None = None,
              need: dict[str, bool] | None = None,
              model_id: str | None = None, only: bool = False,
              role: str = "utility") -> Reply:
    """Ask once. Returns a Reply rather than raising.

    A tool's caller is a model mid-turn, and "no model was available" is
    something to say plainly in a tool result — not an exception that turns into
    a failed turn with no explanation.
    """
    try:
        answer = ask(prompt, system=system, want_json=want_json, media=media,
                     need=need, model_id=model_id, only=only)
    except Exception as err:  # noqa: BLE001 — every failure reads the same here
        return Reply(ok=False, error=str(err))
    return Reply(ok=True, text=answer.text, data=answer.data, model_id=answer.model_id)
