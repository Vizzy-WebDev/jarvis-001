"""Sandbox status over HTTP: which code-isolation backend is active right now.

Read-only and safe to call any time — nothing here starts or changes anything,
it only reports what `jarvis/sandbox/runner.py` already found when it probed
the machine.
"""

from __future__ import annotations

from fastapi import APIRouter

from .. import sandbox

router = APIRouter(prefix="/api")


@router.get("/sandbox/status")
async def sandbox_status() -> dict:
    return sandbox.status()
