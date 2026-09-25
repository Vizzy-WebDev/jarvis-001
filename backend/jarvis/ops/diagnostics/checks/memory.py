"""A create-read-delete canary through the real memory store.

Deliberately SYNCHRONOUS end to end — no await between the three calls — so the
canary is structurally unobservable to anything else rather than merely unlikely
to be seen. Nothing else can interleave and find a memory the user never saved.
"""

from __future__ import annotations

from typing import Any

from ..registry import Check

CANARY_TEXT = "internal health check — not a real memory"


def probe() -> dict[str, Any]:
    from ....memory import store

    created = None
    try:
        created = store.create_memory(category="About You", text=CANARY_TEXT,
                                      source_kind="diagnostic", origin="legacy")
        read_back = store.get_memory(created["id"])
        if read_back is None or read_back["text"] != CANARY_TEXT:
            return {"ok": False, "detail": "A memory was saved but did not read back."}
        return {"ok": True}
    except Exception as err:  # noqa: BLE001
        return {"ok": False, "detail": f"Saving and reading a memory failed: {err}"}
    finally:
        if created is not None:
            try:
                store.delete_memory(created["id"])
            except Exception:  # noqa: BLE001 — a leftover canary is not worth raising over
                pass


CHECK = Check(id="memory", probe=probe)
