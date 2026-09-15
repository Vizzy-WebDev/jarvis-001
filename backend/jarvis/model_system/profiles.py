"""Model profiles (§8) — how Jarvis wants to use a model, kept apart from what
the model itself is.

A profile never duplicates a model definition (§8's own warning): it holds a
model reference (or none at all — Auto), a reasoning level, generation
parameters, and a balance preference. Everything about what that model CAN
do still lives on the one `ai_models` row in `ai/registry.py`; a profile only
says how this particular use of it should be configured.

**Default vs. Auto are different concepts, and a profile is where that shows
up (§9).** The profile flagged `is_default` is what an ordinary request uses
when nothing more specific was asked for. If ITS `model_id` is set, that is a
concrete Default model the person chose. If it is `None`, the person has
chosen Auto as their default behaviour — the router decides every time,
filtered by hard requirements and ranked by preference, exactly as it would
for an explicit one-off Auto request. Both are legitimate; neither is a
special case of the other.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, replace
from typing import Any

from ..db import get_db
from ..jscompat import now_iso
from .parameters import GenerationParams
from .reasoning import Effort
from .request import AIRequest, Preferences

_lock = threading.RLock()

#: Seeded once, the first time this table is read empty. `Fast`/`Balanced`/
#: `Deep Reasoning`/`Coding` are convenience starting points a person can
#: edit or delete freely; `Default` is the one profile that always exists,
#: because SOME profile has to be the one an unqualified request resolves to.
_BUILTIN_SEED: tuple[dict[str, Any], ...] = (
    {"id": "default", "label": "Default", "model_id": None, "reasoning_level": None,
     "balance": "balanced", "is_default": True},
    {"id": "fast", "label": "Fast", "model_id": None, "reasoning_level": Effort.OFF,
     "balance": "fast", "is_default": False},
    {"id": "deep-reasoning", "label": "Deep Reasoning", "model_id": None,
     "reasoning_level": Effort.HIGH, "balance": "quality", "is_default": False},
    {"id": "coding", "label": "Coding", "model_id": None, "reasoning_level": Effort.MEDIUM,
     "balance": "quality", "is_default": False},
)


@dataclass(frozen=True)
class Profile:
    id: str
    label: str
    model_id: str | None
    reasoning_level: Effort | None
    balance: str | None
    params: GenerationParams
    tool_behavior: dict[str, Any]
    is_default: bool
    builtin: bool
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "label": self.label, "modelId": self.model_id,
            "reasoningLevel": self.reasoning_level.name if self.reasoning_level else None,
            "balance": self.balance,
            "params": {k: v for k, v in self.params.__dict__.items() if v not in (None, ())},
            "toolBehavior": dict(self.tool_behavior), "isDefault": self.is_default,
            "builtin": self.builtin, "createdAt": self.created_at,
        }


def _ensure_seeded() -> None:
    db = get_db()
    count = db.execute("SELECT COUNT(*) AS n FROM ai_profiles").fetchone()["n"]
    if count:
        return
    with _lock:
        count = db.execute("SELECT COUNT(*) AS n FROM ai_profiles").fetchone()["n"]
        if count:
            return
        for seed in _BUILTIN_SEED:
            _insert(
                profile_id=seed["id"], label=seed["label"], model_id=seed["model_id"],
                reasoning_level=seed["reasoning_level"], balance=seed["balance"],
                params=GenerationParams(), tool_behavior={}, is_default=seed["is_default"],
                builtin=True,
            )


def _insert(*, profile_id: str, label: str, model_id: str | None, reasoning_level: Effort | None,
           balance: str | None, params: GenerationParams, tool_behavior: dict[str, Any],
           is_default: bool, builtin: bool) -> None:
    db = get_db()
    tool_behavior_stored = {**tool_behavior, "balance": balance}
    params_dict = {k: v for k, v in params.__dict__.items() if v not in (None, ())}
    if is_default:
        db.execute("UPDATE ai_profiles SET is_default = 0")
    db.execute(
        "INSERT INTO ai_profiles (id, label, model_id, reasoning_level, params_json, "
        "tool_behavior_json, is_default, builtin, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (profile_id, label, model_id, reasoning_level.name if reasoning_level else None,
         json.dumps(params_dict), json.dumps(tool_behavior_stored), int(is_default), int(builtin),
         now_iso()),
    )


def _row(row: Any) -> Profile:
    tool_behavior = json.loads(row["tool_behavior_json"] or "{}")
    balance = tool_behavior.pop("balance", None)
    level_name = row["reasoning_level"]
    params_dict = json.loads(row["params_json"] or "{}")
    if "stop_sequences" in params_dict:
        params_dict["stop_sequences"] = tuple(params_dict["stop_sequences"])
    return Profile(
        id=row["id"], label=row["label"], model_id=row["model_id"],
        reasoning_level=Effort[level_name] if level_name else None, balance=balance,
        params=GenerationParams(**params_dict), tool_behavior=tool_behavior,
        is_default=bool(row["is_default"]), builtin=bool(row["builtin"]),
        created_at=row["created_at"],
    )


def list_profiles() -> list[Profile]:
    _ensure_seeded()
    rows = get_db().execute("SELECT * FROM ai_profiles ORDER BY created_at ASC").fetchall()
    return [_row(r) for r in rows]


def get_profile(profile_id: str) -> Profile | None:
    _ensure_seeded()
    row = get_db().execute("SELECT * FROM ai_profiles WHERE id = ?", (profile_id,)).fetchone()
    return _row(row) if row is not None else None


def get_default_profile() -> Profile | None:
    _ensure_seeded()
    row = get_db().execute("SELECT * FROM ai_profiles WHERE is_default = 1").fetchone()
    return _row(row) if row is not None else None


def _slug(seed: str, fallback: str = "profile") -> str:
    base = re.sub(r"(^-|-$)", "", re.sub(r"[^a-z0-9]+", "-", str(seed or fallback).lower().strip()))
    return base or fallback


def add_profile(
    *, label: str, model_id: str | None = None, reasoning_level: Effort | None = None,
    balance: str | None = None, params: GenerationParams | None = None,
    tool_behavior: dict[str, Any] | None = None, is_default: bool = False,
) -> Profile:
    _ensure_seeded()
    if not label:
        raise ValueError("A profile needs a label.")
    with _lock:
        db = get_db()
        existing = {r["id"] for r in db.execute("SELECT id FROM ai_profiles").fetchall()}
        base = _slug(label)
        candidate, n = base, 2
        while candidate in existing:
            candidate = f"{base}-{n}"
            n += 1
        _insert(profile_id=candidate, label=label, model_id=model_id,
               reasoning_level=reasoning_level, balance=balance,
               params=params or GenerationParams(), tool_behavior=tool_behavior or {},
               is_default=is_default, builtin=False)
        return get_profile(candidate)  # type: ignore[return-value]


PATCH_FIELDS = {"label", "model_id", "reasoning_level", "balance", "params", "tool_behavior"}


def update_profile(profile_id: str, patch: dict[str, Any]) -> Profile:
    with _lock:
        db = get_db()
        current = get_profile(profile_id)
        if current is None:
            raise KeyError(f"Unknown profile: {profile_id}")
        fields = {k: v for k, v in patch.items() if k in PATCH_FIELDS}
        sets: list[str] = []
        values: list[Any] = []
        if "label" in fields:
            sets.append("label = ?"); values.append(fields["label"])
        if "model_id" in fields:
            sets.append("model_id = ?"); values.append(fields["model_id"])
        if "reasoning_level" in fields:
            level = fields["reasoning_level"]
            sets.append("reasoning_level = ?")
            values.append(level.name if isinstance(level, Effort) else level)
        if "params" in fields:
            params_dict = {k: v for k, v in (fields["params"] or {}).items() if v not in (None, ())}
            sets.append("params_json = ?"); values.append(json.dumps(params_dict))
        if "balance" in fields or "tool_behavior" in fields:
            tool_behavior = dict(fields.get("tool_behavior", current.tool_behavior))
            tool_behavior["balance"] = fields.get("balance", current.balance)
            sets.append("tool_behavior_json = ?"); values.append(json.dumps(tool_behavior))
        if sets:
            values.append(profile_id)
            db.execute(f"UPDATE ai_profiles SET {', '.join(sets)} WHERE id = ?", values)
        return get_profile(profile_id)  # type: ignore[return-value]


def set_default_profile(profile_id: str) -> Profile:
    with _lock:
        db = get_db()
        if get_profile(profile_id) is None:
            raise KeyError(f"Unknown profile: {profile_id}")
        db.execute("UPDATE ai_profiles SET is_default = 0")
        db.execute("UPDATE ai_profiles SET is_default = 1 WHERE id = ?", (profile_id,))
        return get_profile(profile_id)  # type: ignore[return-value]


def delete_profile(profile_id: str) -> None:
    with _lock:
        db = get_db()
        profile = get_profile(profile_id)
        if profile is None:
            return
        if profile.is_default:
            raise ValueError("The default profile can't be deleted — set a different one as default first.")
        db.execute("DELETE FROM ai_profiles WHERE id = ?", (profile_id,))


def apply_profile(profile: Profile, request: AIRequest) -> AIRequest:
    """Layer a profile onto a request. Whatever the request already specified
    explicitly wins — a profile fills gaps, it never overrides a caller's own
    choice."""
    preferences = request.preferences
    if profile.balance and preferences.balance is None:
        preferences = replace(preferences, balance=profile.balance)

    merged_params = request.params
    if profile.params is not None:
        merged_fields = {}
        for name, value in profile.params.__dict__.items():
            requested = getattr(request.params, name)
            merged_fields[name] = requested if requested not in (None, ()) else value
        merged_params = GenerationParams(**merged_fields)

    return replace(
        request,
        model_id=request.model_id or profile.model_id,
        reasoning=request.reasoning if request.reasoning is not None else profile.reasoning_level,
        params=merged_params,
        preferences=preferences,
    )


def resolve_request(request: AIRequest) -> AIRequest:
    """The request a caller actually sends, after applying whichever profile
    governs it: `request.profile_id` if named, else the default profile, else
    the request unchanged (pure Auto, no profile at all — a fresh install
    with nobody having configured anything still works)."""
    profile = get_profile(request.profile_id) if request.profile_id else get_default_profile()
    return apply_profile(profile, request) if profile is not None else request
