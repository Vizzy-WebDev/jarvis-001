"""OmniRoute's own listing fields, for a connection declared as OmniRoute. Everything
else about it is the plain chat format."""

from __future__ import annotations

from typing import Any

from . import _described


def facts(row: dict[str, Any]) -> dict[str, Any]:
    found = _described.describe(row)
    # OmniRoute marks its own routing/combo entries this way — confirmed live, not
    # documented. It is the only signal it reports that tells a router apart from a model
    # pinned to one upstream provider. Any other owner is it saying "not a router".
    owner = row.get("owned_by")
    if isinstance(owner, str) and owner:
        found["router"] = owner == "combo"
    return found
