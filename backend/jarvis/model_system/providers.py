"""A Provider: a saved address, a wire format, and a credential reference.

A provider answers "how do I reach a service that can supply models" and
nothing about what any specific model can do — that is the registry's job
(`model_system/registry.py`). Two axes meet at a `Model` row exactly the way they do
between a connection and a version: `kind` says what KIND of thing is on the
other end (a fact about the service), `adapter` says which WIRE FORMAT it
speaks (a fact about the code that can talk to it), and the two are
independent — OpenAI, an OpenAI-compatible aggregator, and a local server can
all speak the same wire format while being three different kinds of thing.

**Built-in vs. Custom vs. Local are the same table, never three systems.**
`BUILTIN_TEMPLATES` is data only — a starting point the add-a-provider screen
offers, pre-filling `adapter`/`base_url`/`auth_method` for the services this
build knows about by name. Choosing "Custom" or "Local" starts from a blanker
template in the exact same flow, with the same fields, landing in the exact
same table. Nothing downstream (routing, capability checks, health, usage)
ever branches on `builtin`; it exists solely so the UI can show a padlock on
the handful of fields a well-known provider's template locks (its own
address, its own wire format).
"""

from __future__ import annotations

import re
import threading
from enum import Enum
from typing import Any

from ..db import get_db
from ..jscompat import now_iso
from .credentials import CredentialStatus, status_of

_lock = threading.RLock()


class ProviderKind(Enum):
    """What is genuinely on the other end — independent of wire format."""

    NATIVE = "native"              # a well-known service's own first-party API
    OPENAI_COMPATIBLE = "openai_compatible"  # a third-party OpenAI-shaped endpoint
    AGGREGATOR = "aggregator"      # resells other makers' models (OpenRouter, Groq, ...)
    LOCAL = "local"                # a self-hosted / on-machine endpoint


class AuthMethod(Enum):
    NONE = "none"
    API_KEY = "api_key"
    BEARER = "bearer"


#: Ids a provider may not take — see `model_system/registry.py`'s equivalent for models;
#: same reasoning, so a provider called "recheck" cannot collide with a static
#: route segment under /api/models/providers/<id>.
RESERVED_IDS = frozenset({"discover", "recheck", "health", "usage", "profiles"})

#: The starting points the add-a-provider screen offers. Every field here is
#: exactly what a manually-added Custom or Local provider fills in by hand —
#: this is a convenience default, not a separate code path. `credential_ref`
#: is left `None`; a fresh ref is minted when a real provider row is created
#: from a template (see `add_provider`).
BUILTIN_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "id": "anthropic", "label": "Anthropic", "kind": ProviderKind.NATIVE,
        "adapter": "anthropic", "base_url": None, "auth_method": AuthMethod.API_KEY,
        "url_editable": False, "key_required": True, "discovery_supported": True,
    },
    {
        "id": "openai", "label": "OpenAI", "kind": ProviderKind.NATIVE,
        "adapter": "openai_compatible", "base_url": "https://api.openai.com/v1",
        "auth_method": AuthMethod.API_KEY, "url_editable": False, "key_required": True,
        "discovery_supported": True,
    },
    {
        "id": "gemini", "label": "Google Gemini", "kind": ProviderKind.NATIVE,
        "adapter": "gemini", "base_url": None, "auth_method": AuthMethod.API_KEY,
        "url_editable": False, "key_required": True, "discovery_supported": True,
    },
    {
        "id": "local", "label": "Local server", "kind": ProviderKind.LOCAL,
        "adapter": "openai_compatible", "base_url": "http://localhost:11434/v1",
        "auth_method": AuthMethod.NONE, "url_editable": True, "key_required": False,
        "discovery_supported": True,
    },
    {
        "id": "custom", "label": "Custom", "kind": ProviderKind.OPENAI_COMPATIBLE,
        "adapter": "openai_compatible", "base_url": None, "auth_method": AuthMethod.API_KEY,
        "url_editable": True, "key_required": None, "discovery_supported": True,
    },
)


def get_template(template_id: str) -> dict[str, Any] | None:
    return next((t for t in BUILTIN_TEMPLATES if t["id"] == template_id), None)


class Provider:
    """One configured provider row. A thin view over the `ai_providers` table
    — constructed at read time, never cached, so an edit is visible on the
    very next read the same instant it's saved."""

    __slots__ = (
        "id", "label", "kind", "adapter", "base_url", "auth_method",
        "credential_ref", "key_required", "enabled", "builtin",
        "discovery_supported", "config", "created_at",
    )

    def __init__(self, row: Any) -> None:
        self.id = row["id"]
        self.label = row["label"]
        self.kind = _kind_of(row["kind"])
        self.adapter = row["adapter"]
        self.base_url = row["base_url"]
        self.auth_method = _auth_of(row["auth_method"])
        self.credential_ref = row["credential_ref"]
        self.key_required = None if row["key_required"] is None else bool(row["key_required"])
        self.enabled = bool(row["enabled"])
        self.builtin = bool(row["builtin"])
        self.discovery_supported = (
            None if row["discovery_supported"] is None else bool(row["discovery_supported"])
        )
        import json as _json
        self.config = _json.loads(row["config_json"] or "{}")
        self.created_at = row["created_at"]

    @property
    def credential_status(self) -> CredentialStatus:
        return status_of(self.credential_ref, key_required=self.key_required)

    def as_dict(self) -> dict[str, Any]:
        """The public shape. No `credential_ref` value, ever — only whether
        something is configured."""
        return {
            "id": self.id, "label": self.label, "kind": self.kind.value,
            "adapter": self.adapter, "baseUrl": self.base_url,
            "authMethod": self.auth_method.value,
            "keyRequired": self.key_required, "enabled": self.enabled,
            "builtin": self.builtin, "discoverySupported": self.discovery_supported,
            "credentialStatus": self.credential_status.value,
            "createdAt": self.created_at,
        }


def _kind_of(value: str) -> ProviderKind:
    try:
        return ProviderKind(value)
    except ValueError:
        return ProviderKind.OPENAI_COMPATIBLE


def _auth_of(value: str) -> AuthMethod:
    try:
        return AuthMethod(value)
    except ValueError:
        return AuthMethod.API_KEY


def _slug(seed: str, fallback: str = "provider") -> str:
    base = re.sub(r"(^-|-$)", "", re.sub(r"[^a-z0-9]+", "-", str(seed or fallback).lower().strip()))
    return base or fallback


def _unique_id(seed: str, existing: set[str]) -> str:
    base = _slug(seed)
    taken = existing | RESERVED_IDS
    candidate, n = base, 2
    while candidate in taken:
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def list_providers() -> list[Provider]:
    rows = get_db().execute("SELECT * FROM ai_providers ORDER BY created_at ASC").fetchall()
    return [Provider(r) for r in rows]


def get_provider(provider_id: str) -> Provider | None:
    row = get_db().execute("SELECT * FROM ai_providers WHERE id = ?", (provider_id,)).fetchone()
    return Provider(row) if row is not None else None


def add_provider(
    *,
    label: str,
    kind: ProviderKind,
    adapter: str,
    base_url: str | None = None,
    auth_method: AuthMethod = AuthMethod.API_KEY,
    secret: str | None = None,
    key_required: bool | None = None,
    builtin: bool = False,
    discovery_supported: bool | None = None,
    provider_id: str | None = None,
    config: dict[str, Any] | None = None,
) -> Provider:
    if not label or not adapter:
        raise ValueError("A provider needs a label and an adapter.")
    import json as _json

    from .credentials import set_credential

    with _lock:
        db = get_db()
        existing = {r["id"] for r in db.execute("SELECT id FROM ai_providers").fetchall()}
        if provider_id and provider_id in existing:
            raise ValueError(f"Provider id already in use: {provider_id}")
        pid = provider_id or _unique_id(label, existing)
        ref = f"ai_provider_{pid}" if (secret or auth_method is not AuthMethod.NONE) else None
        if secret and ref:
            set_credential(ref, secret)
        db.execute(
            "INSERT INTO ai_providers (id, label, kind, adapter, base_url, auth_method, "
            "credential_ref, key_required, enabled, builtin, discovery_supported, "
            "config_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
            (pid, label, kind.value, adapter, base_url, auth_method.value, ref,
             None if key_required is None else int(key_required), int(builtin),
             None if discovery_supported is None else int(discovery_supported),
             _json.dumps(config or {}), now_iso()),
        )
        return get_provider(pid)  # type: ignore[return-value]


#: What `update_provider` may change. `adapter` and `kind` are absent on
#: purpose — changing either makes this a provider of something else, and
#: every model hanging off it would silently be reinterpreted through a wire
#: format it was never added under. Remove and re-add is the honest way to do
#: that, matching the same rule the model registry places on a deployment's
#: connection.
PATCH_FIELDS = {"label", "base_url", "key_required", "enabled", "config"}


def update_provider(provider_id: str, patch: dict[str, Any]) -> Provider:
    import json as _json

    from .credentials import set_credential

    with _lock:
        db = get_db()
        current = get_provider(provider_id)
        if current is None:
            raise KeyError(f"Unknown provider: {provider_id}")

        secret = patch.get("secret")
        ref = current.credential_ref
        if secret:
            ref = ref or f"ai_provider_{provider_id}"
            set_credential(ref, secret)

        sets: list[str] = []
        values: list[Any] = []
        fields = {k: v for k, v in patch.items() if k in PATCH_FIELDS}
        if "label" in fields:
            sets.append("label = ?"); values.append(fields["label"])
        if "base_url" in fields:
            sets.append("base_url = ?"); values.append(fields["base_url"])
        if "key_required" in fields:
            kr = fields["key_required"]
            sets.append("key_required = ?"); values.append(None if kr is None else int(bool(kr)))
        if "enabled" in fields:
            sets.append("enabled = ?"); values.append(int(bool(fields["enabled"])))
        if "config" in fields:
            sets.append("config_json = ?"); values.append(_json.dumps(fields["config"] or {}))
        if ref != current.credential_ref:
            sets.append("credential_ref = ?"); values.append(ref)
        if sets:
            values.append(provider_id)
            db.execute(f"UPDATE ai_providers SET {', '.join(sets)} WHERE id = ?", values)
        return get_provider(provider_id)  # type: ignore[return-value]


def remove_provider(provider_id: str, *, keep_credential: bool = False) -> int:
    """Removes the provider and, via `ON DELETE CASCADE`, every model
    registered under it. Returns how many models went. Does not remove a
    credential shared under a legacy ref by convention — callers that mint
    their own refs never collide with one."""
    from .credentials import clear_credential

    with _lock:
        db = get_db()
        current = get_provider(provider_id)
        if current is None:
            return 0
        count = db.execute(
            "SELECT COUNT(*) AS n FROM ai_models WHERE provider_id = ?", (provider_id,)
        ).fetchone()["n"]
        db.execute("DELETE FROM ai_providers WHERE id = ?", (provider_id,))
        if current.credential_ref and not keep_credential:
            clear_credential(current.credential_ref)
        return int(count)
