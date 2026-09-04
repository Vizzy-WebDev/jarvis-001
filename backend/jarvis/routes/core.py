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

    Reports False until the models registry is ported (Wave 2). That is honest
    for a fresh install with no key configured, which is the only state the
    contract fixtures currently cover; it must be revisited the moment
    listModels()/isReady() land, and the fixture for a CONFIGURED instance is
    what will catch it if it is not.
    """
    return {"configured": False}


@router.get("/prefs")
def read_prefs() -> dict[str, Any]:
    return get_prefs()


@router.post("/prefs")
def write_prefs(patch: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return set_prefs(patch or {})
