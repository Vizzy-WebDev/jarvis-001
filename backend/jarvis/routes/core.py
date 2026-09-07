"""Status and preferences — the smallest real slice of the API.

Deliberately ported first: between them these two exercise every layer the rest
of the port sits on (JSON store round-trip, defaults merging, response shape)
while depending on nothing that is still unported, so the contract-replay harness
can be proven end to end before the large waves start.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body

from ..gateway.registry import is_ready, list_models
from ..prefs import get_prefs, set_prefs

router = APIRouter(prefix="/api")


@router.get("/status")
def status() -> dict[str, Any]:
    """Whether any model is actually usable — what the first-run flow checks.

    "Configured" means at least one enabled model has whatever credential it
    actually needs. Deliberately not "a connection exists": a saved connection
    whose key was never entered is exactly the state this flow exists to catch,
    and reporting it as configured sends the user to a chat box that cannot
    answer.
    """
    usable = any(m.get("enabled", True) and is_ready(m) for m in list_models())
    # Exactly the recorded shape, deliberately: the contract harness only catches
    # unintended divergence if the intended response stays byte-identical too.
    # Counts belong here when a screen actually needs them, not before.
    return {"configured": usable}


@router.get("/prefs")
def read_prefs() -> dict[str, Any]:
    return get_prefs()


@router.post("/prefs")
def write_prefs(patch: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return set_prefs(patch or {})
