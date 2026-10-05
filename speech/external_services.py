"""Keys for services that are not model providers.

A "service" is any name the user types — Deepgram, ElevenLabs, whatever comes
next — plus a key and an optional second field (a Voice ID, say). There is no
fixed list here to extend for a new one.

**Deliberately separate from model-provider keys.** A model connection's secret
is managed entirely through the models routes and never through here, so a bug
in this file cannot corrupt a connection that is currently answering.

**The storage split matches the rest of the project.** Non-sensitive STRUCTURE —
a service's ref, its label, whether it declares a second field — lives in plain
JSON; every actual credential value goes through `config.save_secret`. "One
active key per service" falls out of that for free: saving a secret overwrites
one entry, so there is nowhere for old keys to stockpile.
"""

from __future__ import annotations

import re
from typing import Any

from .config import delete_secret, get_secret, save_secret
from .store import read_json, write_json

FILE = "external-services"


def _slug(label: str) -> str:
    return re.sub(r"(^-+|-+$)", "", re.sub(r"[^a-z0-9]+", "-", str(label or "").strip().lower()))


def _extra_ref(ref: str) -> str:
    return f"{ref}_extra"


def _registry() -> dict[str, Any]:
    data = read_json(FILE, {})
    if not isinstance(data, dict):
        return {}
    # One-time and idempotent: the hardcoded version that came before this saved
    # a real Deepgram key with no row to go with it. Without this the key keeps
    # working but vanishes from the list until someone re-adds it under exactly
    # the same name. A no-op the moment a real row exists.
    if "deepgram" not in data and get_secret("deepgram"):
        data["deepgram"] = {"label": "Deepgram", "extraFieldLabel": None}
        write_json(FILE, data)
    return data


def list_services() -> list[dict[str, Any]]:
    """Every known service with its live connected state — what a row renders
    from. Never the key itself."""
    out = []
    for ref, entry in _registry().items():
        extra_label = entry.get("extraFieldLabel")
        out.append({
            "ref": ref,
            "label": entry.get("label"),
            "configured": bool(get_secret(ref)),
            "extraFieldLabel": extra_label or None,
            "extraFieldConfigured": bool(get_secret(_extra_ref(ref))) if extra_label else False,
        })
    return out


def get_service(ref: str) -> dict[str, Any] | None:
    return next((s for s in list_services() if s["ref"] == ref), None)


def add_or_update(*, label: str, key: str, extra_field_label: str | None = None,
                  extra_field_value: str | None = None,
                  allow_update: bool = True) -> dict[str, Any]:
    """Add a new service, or replace an existing one's key.

    `allow_update` is what separates the two real callers, and it exists because
    of a confirmed bug: typing the name of an already-connected service into the
    ADD form silently overwrote that service's key, with no error and nothing to
    show it had happened. The add route passes `False`, so any collision is a
    clear refusal; a row's own Save passes `True`, where overwriting IS the
    intent.

    `extra_field_*` are taken as given every time. There is no hidden merge with
    what was there before — the caller re-sends what should stay.
    """
    label = str(label or "").strip()
    if not label:
        raise ValueError("A service name is required.")
    key = str(key or "").strip()
    if not key:
        raise ValueError("A key is required.")

    ref = _slug(label)
    if not ref:
        raise ValueError("That name doesn’t produce a usable service id — "
                         "try including a letter or number.")

    data = _registry()
    existing = data.get(ref)
    if existing and not allow_update:
        raise ValueError(
            f'"{existing.get("label")}" is already connected. Use its own controls to change '
            "its key, or choose a different name for a genuinely new service.")
    if existing and str(existing.get("label", "")).lower() != label.lower():
        raise ValueError(
            f'A service named "{existing.get("label")}" already exists with a very similar '
            "name. Choose a different name, or update that one instead.")

    extra_label = str(extra_field_label or "").strip()
    data[ref] = {"label": label, "extraFieldLabel": extra_label or None}
    write_json(FILE, data)

    save_secret(ref, key)
    extra_value = str(extra_field_value or "").strip()
    if extra_label and extra_value:
        save_secret(_extra_ref(ref), extra_value)
    else:
        # Either the row no longer declares a second field, or it does and was
        # left empty. Nothing orphaned is left behind under it.
        delete_secret(_extra_ref(ref))

    saved = get_service(ref)
    assert saved is not None
    return saved


def remove_key(ref: str) -> None:
    """Clear the key but keep the row: it goes back to "not connected" under the
    same name, ready for a new one."""
    if not ref:
        return
    delete_secret(ref)
    delete_secret(_extra_ref(ref))


def delete_service(ref: str) -> None:
    """Remove the row entirely — name and all, not just its key."""
    if not ref:
        return
    remove_key(ref)
    data = _registry()
    if ref in data:
        del data[ref]
        write_json(FILE, data)


def get_key(ref: str) -> str | None:
    return get_secret(ref)


def get_extra_field(ref: str) -> str | None:
    return get_secret(_extra_ref(ref))
