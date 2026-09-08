"""One Client ID/Secret per catalog entry, registered ONCE by the user and
then shared by every connector that catalog entry ever creates for this
Jarvis install — the closest a single-user, no-backend install can get to
what a real product does when it registers one OAuth client centrally for
every user (Claude's own Gmail/Drive connectors work this way; Anthropic did
that registration once, invisibly, and no individual Claude user ever sees a
Client ID field for them). Google's/GitHub's/Slack's real OAuth servers don't
support automatic registration (verified live in the Node build — no
`registration_endpoint`), so SOME registration has to happen somewhere; this
is what makes it happen exactly once per catalog entry instead of once per
connector.

Same minimal leaf-module pattern as `store.py`: `read_json`/`write_json` from
`jarvis/store.py`, dependency-free, nothing here imports `capabilities.py` or
any of the forbidden circular-import targets (see root CLAUDE.md's Gotchas).
The Client ID (not secret) lives in `data/catalog-credentials.json` — git-
ignored, same as everything else under `data/` — never in the bundled,
hand-verified `catalog.json`. The Client SECRET never touches this file at
all; it goes through the same `save_secret()`/`get_secret()` every other
credential in this codebase already uses, under a ref namespaced to the
catalog entry (`catalogclient_<catalogId>`), so it lands in `.env`, not JSON.
"""

from __future__ import annotations

from ..config import delete_secret, get_secret, save_secret
from ..store import read_json, write_json

FILE = "catalog-credentials"  # data/catalog-credentials.json


def _secret_ref(catalog_id: str) -> str:
    return f"catalogclient_{catalog_id}"


def get_catalog_client(catalog_id: str) -> dict[str, str] | None:
    """The registered Client ID for a catalog entry, or None if none has been
    saved yet. Never returns the secret — callers needing it call
    `get_catalog_client_secret()` separately."""
    client_id = (read_json(FILE, {}) or {}).get(catalog_id, {}).get("clientId")
    return {"clientId": client_id} if client_id else None


def get_catalog_client_secret(catalog_id: str) -> str | None:
    return get_secret(_secret_ref(catalog_id))


def save_catalog_client(catalog_id: str, client_id: str, client_secret: str | None) -> None:
    """Registers (or replaces) the Client ID/Secret for one catalog entry —
    called once by the user, through the same guided-setup/manual-Client-ID
    modal that already exists, never a new screen."""
    all_entries = dict(read_json(FILE, {}) or {})
    all_entries[catalog_id] = {"clientId": client_id}
    write_json(FILE, all_entries)
    if client_secret:
        save_secret(_secret_ref(catalog_id), client_secret)


def clear_catalog_client(catalog_id: str) -> None:
    """Clears a catalog entry's registered client — for the day a Google
    Cloud project (or similar) gets deleted or rotated and the user wants to
    register a fresh one."""
    all_entries = dict(read_json(FILE, {}) or {})
    all_entries.pop(catalog_id, None)
    write_json(FILE, all_entries)
    delete_secret(_secret_ref(catalog_id))
