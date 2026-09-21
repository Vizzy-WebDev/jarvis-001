"""Status and preferences — the smallest real slice of the API.

Deliberately ported first: between them these two exercise every layer the rest
of the port sits on (JSON store round-trip, defaults merging, response shape)
while depending on nothing that is still unported, so the contract-replay harness
can be proven end to end before the large waves start.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body

from ..prefs import get_prefs, set_prefs

router = APIRouter(prefix="/api")


@router.get("/status")
def status() -> dict[str, Any]:
    """Whether any model is actually usable — what the first-run flow checks.
    Nothing is, while there is no AI model system."""
    usable = False
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
