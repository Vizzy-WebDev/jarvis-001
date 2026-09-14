"""Which model does which job.

Jarvis has always had distinct jobs for a model and never had a way to say so.
The router already scores a control-loop turn differently from a conversation
and a background turn differently again; two preferences existed for pinning a
voice model and a manual model, and were read by nothing. The need was
identified and the wiring was never built, so the answer to "which model is
answering me" was whatever won a scoring tie-break.

**A slot leads the ranking. It never restricts it.** The obvious implementation
of "voice uses this model" is a filter, and a filter means a voice turn FAILS
when that one deployment is rate-limited — trading an occasional slightly-worse
answer for an occasional no answer at all. So an assignment resolves to a pin,
and `routing.build_candidates` already honours a pin by moving it to the front
rather than by removing everything else. An assignment that is benched,
switched off or deleted therefore degrades to ordinary ranking rather than
breaking the turn, and nothing here has to remember to check.

**Every slot is optional and unassigned is the normal state.** A fresh install
with one model needs no configuration at all; the roles exist so that someone
who wants to say "spoken replies should be quick, the screen-driving loop should
be careful" has somewhere to say it.

The five roles are Jarvis's own, derived from what the code already
distinguishes rather than borrowed from a larger harness's list. Three of them
are already separate scoring branches, one already had a preference reserving
its name, and the fifth is the eighteen one-off `ask()` callers, which are
uniformly cheap, background and JSON-shaped.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..catalog import Effort
from ..store import read_json, write_json

FILE = "model-slots"


class Role(Enum):
    """A job a model gets asked to do.

    Each corresponds to something the system already treats differently, not to
    a category invented for the sake of having one.
    """

    #: A person is waiting for the answer. The ordinary chat turn.
    CONVERSATION = "conversation"
    #: Spoken. Latency is most of the experience, so this is the one role where
    #: a faster, weaker model is often the RIGHT answer rather than a compromise.
    VOICE = "voice"
    #: Driving the screen. A wrong click costs more than a slightly clumsy
    #: sentence, and nobody is watching it happen in real time.
    CONTROL = "control"
    #: Scheduled tasks, job workers, briefings. Nobody is waiting, so cost
    #: matters more than latency.
    BACKGROUND = "background"
    #: The one-off asks — memory extraction, verification, heartbeat triage,
    #: improvement synthesis. Small, frequent, and answered in JSON.
    UTILITY = "utility"


@dataclass(frozen=True)
class Slot:
    """A role, and what the user has said about how it should be served."""

    role: Role
    #: A deployment id. May name something that no longer exists — a slot is a
    #: preference, and a stale preference is not an error.
    deployment_id: str | None = None
    #: The reasoning level to ask for by default in this role. `None` means use
    #: whatever the chosen version's own default is.
    effort: Effort | None = None

    @property
    def assigned(self) -> bool:
        return self.deployment_id is not None or self.effort is not None


_lock = threading.RLock()
_cache: dict[str, dict[str, Any]] | None = None


def _load() -> dict[str, dict[str, Any]]:
    global _cache
    with _lock:
        if _cache is None:
            stored = read_json(FILE, None)
            if stored is None:
                _cache = _adopt_dormant_preferences()
                if _cache:
                    write_json(FILE, {"slots": _cache})
            else:
                _cache = stored.get("slots", {}) if isinstance(stored, dict) else {}
        return _cache


def _flush() -> None:
    """Write through. Called with the lock held."""
    write_json(FILE, {"slots": _cache or {}})


def _adopt_dormant_preferences() -> dict[str, dict[str, Any]]:
    """Honour the two pins that existed as preferences and were never read.

    `manualModelId` and `voiceModelId` were stored, returned by the preferences
    route, and consulted by nothing — so anyone who set one has been running for
    however long with a setting that silently did nothing. Adopting them is
    honouring an intent that was dropped, which is different from keeping a
    field because it happens to exist.

    The ids are old model-row ids. They may not name a deployment, and that is
    handled the same way as any other stale assignment: it degrades to ranking.
    """
    from ..prefs import forget, get_prefs

    try:
        prefs = get_prefs()
    except Exception:  # noqa: BLE001 — a slot store must not fail to load over this
        return {}

    adopted: dict[str, dict[str, Any]] = {}
    for role, key in ((Role.CONVERSATION, "manualModelId"), (Role.VOICE, "voiceModelId")):
        value = prefs.get(key)
        if isinstance(value, str) and value:
            adopted[role.value] = {"deploymentId": value}

    # Swept up whether or not anything was adopted: `autoSelect` has no new home
    # and a key left behind goes on being served to a screen that might still
    # offer it.
    try:
        forget("manualModelId", "voiceModelId", "autoSelect")
    except Exception:  # noqa: BLE001 — tidying must not break loading
        pass
    return adopted


def _as_effort(value: Any) -> Effort | None:
    if value is None:
        return None
    if isinstance(value, Effort):
        return value
    try:
        return Effort[str(value).upper()]
    except KeyError:
        return None


def get(role: Role) -> Slot:
    """The slot for a role. Always answers; unassigned is a real answer."""
    row = _load().get(role.value) or {}
    return Slot(
        role=role,
        deployment_id=row.get("deploymentId") or None,
        effort=_as_effort(row.get("effort")),
    )


def all_slots() -> dict[Role, Slot]:
    return {role: get(role) for role in Role}


def assign(role: Role, *, deployment_id: str | None = None,
           effort: Effort | None = None) -> Slot:
    """Say how a role should be served.

    Both parts are independent: assigning a model without an effort leaves the
    version's own default in place, and assigning an effort without a model says
    "whatever gets picked, ask it to think this hard" — which is the more useful
    of the two for anyone who has not got a favourite model.
    """
    with _lock:
        slots = _load()
        row: dict[str, Any] = dict(slots.get(role.value) or {})
        if deployment_id is not None:
            row["deploymentId"] = deployment_id or None
        if effort is not None:
            row["effort"] = effort.name
        slots[role.value] = {k: v for k, v in row.items() if v is not None}
        _flush()
    return get(role)


def clear(role: Role) -> None:
    """Hand a role back to ordinary ranking."""
    with _lock:
        slots = _load()
        if slots.pop(role.value, None) is not None:
            _flush()


def forget_deployment(deployment_id: str) -> int:
    """Drop every assignment naming a deployment that has gone.

    Not strictly required — a dangling assignment already degrades to ranking —
    but a screen that lists what each role is set to should not go on naming
    something the user deleted, and an id freed for reuse would otherwise hand
    the next deployment a role it was never given.
    """
    removed = 0
    with _lock:
        slots = _load()
        for role_value, row in list(slots.items()):
            if row.get("deploymentId") == deployment_id:
                rest = {k: v for k, v in row.items() if k != "deploymentId"}
                if rest:
                    slots[role_value] = rest
                else:
                    slots.pop(role_value)
                removed += 1
        if removed:
            _flush()
    return removed


def pin_for(role: Role) -> str | None:
    """The deployment this role prefers, as a PIN for the router.

    Named for what it is. A pin is honoured by moving a candidate to the front
    of the ranking, never by removing the others — so this cannot become a
    filter by accident at the call site.
    """
    return get(role).deployment_id


def effort_for(role: Role) -> Effort | None:
    """The reasoning level this role asks for, or None for the version's own."""
    return get(role).effort


def reset_for_tests() -> None:
    global _cache
    with _lock:
        _cache = None
