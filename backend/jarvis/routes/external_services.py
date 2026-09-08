"""Keys for services that are not model providers, over HTTP.

A key goes IN on these routes and never comes back out: every response is the
service's public row — its name, whether a key is set — and never the value.

The live-test route is deliberately a 501 rather than a pretend pass when no
tester exists for a service: "we could not check" and "it works" are different
answers, and a green tick nobody earned is worse than no tick.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from .. import external_services

router = APIRouter(prefix="/api/external-services")


@router.get("")
def listed() -> dict[str, Any]:
    return {"services": external_services.list_services()}


@router.post("")
def add(body: dict[str, Any] = Body(default_factory=dict)):
    try:
        # allow_update False: this route is "add something NEW". A collision is
        # a clear error, never a silent overwrite of a working key through the
        # wrong form — see the store's own note on the bug that caused.
        service = external_services.add_or_update(
            label=body.get("label") or "", key=body.get("key") or "",
            extra_field_label=body.get("extraFieldLabel"),
            extra_field_value=body.get("extraFieldValue"), allow_update=False)
    except ValueError as err:
        return JSONResponse({"ok": False, "error": str(err)}, status_code=400)
    return {"ok": True, "service": service}


@router.post("/{ref}")
def update(ref: str, body: dict[str, Any] = Body(default_factory=dict)):
    existing = external_services.get_service(ref)
    if existing is None:
        return JSONResponse({"error": "Unknown service."}, status_code=404)
    try:
        service = external_services.add_or_update(
            label=str(existing["label"]), key=body.get("key") or "",
            extra_field_label=body.get("extraFieldLabel"),
            extra_field_value=body.get("extraFieldValue"))
    except ValueError as err:
        return JSONResponse({"ok": False, "error": str(err)}, status_code=400)
    return {"ok": True, "service": service}


@router.delete("/{ref}")
def clear_key(ref: str):
    if external_services.get_service(ref) is None:
        return JSONResponse({"error": "Unknown service."}, status_code=404)
    external_services.remove_key(ref)
    return {"ok": True}


@router.delete("/{ref}/full")
def remove(ref: str):
    if external_services.get_service(ref) is None:
        return JSONResponse({"error": "Unknown service."}, status_code=404)
    external_services.delete_service(ref)
    return {"ok": True}


@router.post("/{ref}/test")
def test(ref: str, body: dict[str, Any] = Body(default_factory=dict)):
    """Check a key against the real service.

    The value in the request body when one is given, so a key can be tested
    before it is saved; otherwise whatever is already stored, so this doubles as
    "is this still valid" after a provider-side rotation.

    No provider has a tester in this build yet — the speech and voice adapters
    arrive with the voice wave — so this is honestly a 501 rather than a pass.
    """
    if external_services.get_service(ref) is None:
        return JSONResponse({"error": "Unknown service."}, status_code=404)
    return JSONResponse(
        {"ok": False, "error": "No live test is available for this service yet."},
        status_code=501)
