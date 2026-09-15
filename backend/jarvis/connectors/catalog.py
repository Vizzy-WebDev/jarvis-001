"""The bundled directory of apps that are known to work.

Each entry is `{id, label, icon, description, connectFlow}`, and its
`connectFlow` drops straight into a connector's own config — so connecting from
the catalogue runs the SAME flow a custom connector does, just pre-filled. There
is no "ready" versus "needs setup" badge anywhere: every entry gets the same
plain Connect button, and what actually differs is handled by the flow itself.

**An entry is only listed once its real endpoint has been verified live**, not
recalled from documentation. That rule is why this file is short. Two flow kinds
exist because two things are genuinely true of the real services: one registers
a client automatically, and four need a client created by hand in that service's
own console first, which their own steps walk through.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CATALOG_FILE = Path(__file__).with_name("catalog.json")


def _read() -> list[dict[str, Any]]:
    try:
        parsed = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        logger.warning("could not read the connector catalogue: %s", err)
        return []
    entries = parsed.get("entries")
    return entries if isinstance(entries, list) else []


#: Read once: this ships with the app and does not change while it runs.
_ENTRIES = _read()


def list_catalog() -> list[dict[str, Any]]:
    return list(_ENTRIES)


def get_entry(entry_id: str) -> dict[str, Any] | None:
    return next((entry for entry in _ENTRIES if entry.get("id") == entry_id), None)
