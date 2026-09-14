"""Which model does which job — the surface over `gateway/slots.py`.

Its own router rather than a path under `/api/models` for a reason the models
routes already demonstrate: `/api/models/<id>` shares a path space with every
static segment beside it, so each one added there is one more id a deployment
may not have. A role is not a model anyway — it is a statement about how a kind
of work should be served, and the deployment it names may not even exist.

**An assignment is a preference, so nothing here validates one against the
roster.** Naming a deployment that was deleted, or one that is currently
rate-limited, is not an error: `routing.build_candidates` honours a slot by
moving it to the front, never by removing everything else, so a stale or
benched assignment degrades to ordinary ranking. Refusing the assignment here
would be stricter than the router and would turn a preference into a
constraint — which is exactly the failure the pin design avoids.

What this route DOES report is whether the assignment currently resolves, so a
screen can say "that model is no longer here" without the API having to decide
that on the user's behalf.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from ..catalog import Effort
from ..gateway import deployments, slots
from ..gateway.slots import Role

router = APIRouter(prefix="/api/roles")

#: The public shape of one role. Declared rather than spread from the stored
#: row, for the reason `routes/models.py` gives at `MODEL_FIELDS`: a field added
#: to storage must not appear on the wire unannounced.
#:
#: Unlike there, this is not enforced by construction — `_public_role` builds
#: the dict literally, which IS the declaration. This list is what
#: `tests/test_routes_roles.py` asserts the response against, so the two can
#: only disagree by someone editing one and watching a test fail.
ROLE_KEYS = ("id", "label", "description", "deploymentId", "deployment", "effort",
             "effortChoices", "assigned")

#: What each role is FOR, in the words a person would use. The enum's own docs
#: say the same thing to a reader of the code; this is the half a screen needs,
#: and keeping it here rather than in `slots.py` keeps presentation out of the
#: store.
DESCRIPTIONS: dict[Role, tuple[str, str]] = {
    Role.CONVERSATION: ("Chat", "When you are typing and waiting for the answer."),
    Role.VOICE: ("Speaking", "Spoken replies. A faster model is often the better "
                             "choice here, not a compromise."),
    Role.CONTROL: ("Driving your computer", "A wrong click costs more than a clumsy "
                                            "sentence, and nobody is watching in real time."),
    Role.BACKGROUND: ("Scheduled work", "Tasks and jobs that run while you are away."),
    Role.UTILITY: ("Small internal asks", "Remembering things, checking its own work, "
                                          "triaging what it noticed."),
}


def _public_effort(version: Any) -> list[dict[str, Any]]:
    """The levels THIS version actually offers, lowest first.

    Read from the version rather than listed as a fixed ladder, because the
    ladders genuinely differ: the OpenAI-shaped wire accepts a level above
    `high` and Gemini's enum does not. A picker offering a level the chosen
    model cannot take would be offering a setting that silently clamps.

    Empty is a real answer, and means this model has no reasoning control — or
    that nobody has established whether it has any. Both are states a screen
    should render as "not available for this model" rather than as an empty
    dropdown that looks broken.
    """
    scheme = getattr(version, "effort", None)
    if scheme is None or not scheme.controllable:
        return []
    return [{"id": level.name, "label": level.name.title()} for level in scheme.levels]


def _public_role(role: Role, by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    slot = slots.get(role)
    label, description = DESCRIPTIONS[role]
    entry = by_id.get(slot.deployment_id or "")
    version = deployments.version_of(entry) if entry else None
    return {
        "id": role.value,
        "label": label,
        "description": description,
        "deploymentId": slot.deployment_id,
        # None when the id names nothing — a deleted model, or a pin adopted
        # from an old preference. The id is still reported, so a screen can say
        # what it was rather than pretending nothing was ever set.
        "deployment": None if entry is None else {
            "id": entry["id"],
            "label": entry.get("label") or entry.get("model"),
            "model": entry.get("model"),
            "connectionLabel": entry.get("connectionLabel"),
            "enabled": bool(entry.get("enabled", True)),
            "ready": deployments.is_ready(entry),
        },
        "effort": slot.effort.name if slot.effort is not None else None,
        "effortChoices": _public_effort(version),
        "assigned": slot.assigned,
    }


def _role_or_none(value: str) -> Role | None:
    try:
        return Role(str(value).strip().lower())
    except ValueError:
        return None


@router.get("")
def listed() -> dict[str, Any]:
    """Every role, set or not.

    All five always, because the unassigned ones are the ones a person most
    needs to see in order to set them — a list that showed only what had been
    configured would hide the entire feature from anyone who has not used it.
    """
    by_id = {d["id"]: d for d in deployments.list_deployments()}
    return {"roles": [_public_role(role, by_id) for role in Role]}


@router.put("/{role_id}")
def assign(role_id: str, body: dict[str, Any] = Body(default_factory=dict)):
    """Set either half, or both.

    `deploymentId: null` clears the model and leaves any effort in place;
    `effort: null` does the reverse. They are independent because assigning an
    effort without a model is the more useful half for anyone without a
    favourite model: "whatever gets picked, ask it to think this hard".
    """
    role = _role_or_none(role_id)
    if role is None:
        return JSONResponse({"ok": False, "error": "There is no job by that name."},
                            status_code=404)

    if "deploymentId" in body:
        chosen = str(body["deploymentId"] or "") or None
        if chosen is None:
            slots.clear_model(role)
        else:
            slots.assign(role, deployment_id=chosen)
    if "effort" in body:
        level = body["effort"]
        if level is None:
            slots.clear_effort(role)
        else:
            try:
                slots.assign(role, effort=Effort[str(level).upper()])
            except KeyError:
                return JSONResponse({"ok": False, "error": f"There is no level called {level!r}."},
                                    status_code=400)

    by_id = {d["id"]: d for d in deployments.list_deployments()}
    return {"ok": True, "role": _public_role(role, by_id)}


@router.delete("/{role_id}")
def clear(role_id: str):
    """Hand a role back to ordinary ranking."""
    role = _role_or_none(role_id)
    if role is None:
        return JSONResponse({"ok": False, "error": "There is no job by that name."},
                            status_code=404)
    slots.clear(role)
    by_id = {d["id"]: d for d in deployments.list_deployments()}
    return {"ok": True, "role": _public_role(role, by_id)}
