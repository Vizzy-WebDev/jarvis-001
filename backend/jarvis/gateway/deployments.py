"""A deployment: one model version reached through one connection.

This is the routable unit — the thing the router actually chooses between, and
the thing a user actually created. It replaces the flat model row, whose
problem was not its fields but its identity: a row WAS a model, so a model
reachable two ways had to be two unrelated rows that nothing connected, and a
model reachable one way had nowhere to record which of its facts came from the
provider and which from a regex over its name.

Two axes cross here and neither contains the other:

* the **catalog** says what the model is — provider, family, version,
  capabilities, what reasoning control it offers
* the **connection** says how to reach it — address, credential, wire format

A deployment is the pairing, and it owns what belongs to the pairing rather than
to either side: whether the user has it switched on, what it has cost when
called through THIS connection, and whether it is currently in cooldown.

**Availability stays keyed here, and that is not an implementation detail.**
It would read more naturally in the new structure to bench a version — the model
is the thing that failed, after all. It would also mean one rate-limited gateway
route taking the same model offline when reached with your own key, which is
exactly the behaviour the crossing axis exists to make impossible.
`tests/test_deployments.py` asserts it directly, because the version-keyed
version of this code passes every other test in the file.

Four fields from the old row are deliberately gone rather than ported:
`tier.cost` and `tier.speed` (both measurable, and a measured number beats an
authored one), `tags` (derived from those guesses, read by nothing) and
`billing` (a name regex that `kind == "local"` already answers better). What
survives of `tier` is a single coarse quality signal, and it lives on the
version in the catalog, not here.
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from typing import Any

from ..catalog import Version, resolve
from ..config import get_secret
from ..store import read_json, write_json
from .connections import get_connection, list_connections
from .providers import provider_for_legacy

FILE = "model-deployments"

#: What `update_deployment` may change.
#:
#: `connectionId` and `model` are absent on purpose, as `adapter`/`baseUrl`/
#: `secretRef` were absent from the old row's patch keys and for the same
#: reason: changing either would make this a deployment of something else, and
#: silently carry its cooldown history and cost record across to a different
#: model. Removing and re-adding is the honest way to do that.
PATCH_KEYS = ("label", "enabled", "notes", "overrides")

_lock = threading.RLock()


def _load() -> dict[str, Any]:
    data = read_json(FILE, {"deployments": []})
    if not isinstance(data, dict) or not isinstance(data.get("deployments"), list):
        return {"deployments": []}
    return data


def _save(data: dict[str, Any]) -> None:
    write_json(FILE, data)


def _connections_by_id() -> dict[str, dict[str, Any]]:
    return {c["id"]: c for c in list_connections() if c.get("id")}


def hydrate(entry: dict[str, Any], by_id: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Fill in the connection's facts and the catalog's answer, at READ time.

    Kept from the old registry because the reasoning still holds: a stored copy
    of the connection's address drifts the moment the connection is edited, and
    a stored copy of what a model can do cannot pick up a capability the catalog
    learned about afterwards. Neither is written down, so neither can be stale.

    The connection's `provider` and the version's `provider` are DIFFERENT
    things and are named differently here. A gateway connection might be set up
    as "custom" while serving somebody else's model; calling both of them
    `provider` is how the crossing axis quietly collapses back into one.
    """
    by_id = _connections_by_id() if by_id is None else by_id
    conn = by_id.get(entry.get("connectionId") or "") or {}
    kind = conn.get("kind")
    if kind is None and conn:
        kind = provider_for_legacy(conn.get("adapter"), conn.get("baseUrl"))["kind"]

    version = resolve(
        model=entry.get("model") or "",
        discovered=entry.get("discovered") or {},
        user=entry.get("overrides") or {},
    )

    hydrated = dict(entry)
    hydrated.update({
        # Flat, and named exactly as they are today: an adapter is handed this
        # dict directly and reads these keys off it.
        "adapter": conn.get("adapter"),
        "baseUrl": conn.get("baseUrl"),
        "secretRef": conn.get("secretRef"),
        "keyRequired": conn.get("keyRequired"),
        "kind": kind,
        "connectionProvider": conn.get("provider"),
        "connectionLabel": conn.get("label"),
        # What the model IS, resolved from the catalog with provenance attached.
        "version": version,
    })
    return hydrated


def list_deployments() -> list[dict[str, Any]]:
    by_id = _connections_by_id()
    return [hydrate(e, by_id) for e in _load()["deployments"]]


def get_deployment(deployment_id: str) -> dict[str, Any] | None:
    entry = next((e for e in _load()["deployments"] if e.get("id") == deployment_id), None)
    return hydrate(entry) if entry else None


def _make_id(seed: str, existing: list[dict[str, Any]]) -> str:
    base = re.sub(r"(^-|-$)", "", re.sub(r"[^a-z0-9]+", "-", str(seed or "model").lower().strip())) or "model"
    taken = {e.get("id") for e in existing}
    candidate, n = base, 2
    while candidate in taken:
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def add_deployment(
    *,
    connection_id: str,
    model: str,
    label: str | None = None,
    enabled: bool = True,
    discovered: dict[str, Any] | None = None,
    overrides: dict[str, Any] | None = None,
    notes: str = "",
) -> dict[str, Any]:
    """Add one model, reachable through one connection.

    Nothing is guessed and stored here. What the provider said at add time goes
    in `discovered`, what the user corrected goes in `overrides`, and everything
    else is worked out at read time by the catalog — so a deployment added today
    picks up a better answer tomorrow without anybody migrating a file.
    """
    if not connection_id or not model:
        raise ValueError("A deployment needs a connection and a model name.")
    conn = get_connection(connection_id)
    if conn is None:
        raise KeyError(f"Unknown connection: {connection_id}")

    with _lock:
        data = _load()
        # (connection, model), never model alone — the same model legitimately
        # exists twice under two different keys, which is the entire point.
        if any(e.get("connectionId") == connection_id and e.get("model") == model
               for e in data["deployments"]):
            raise ValueError(f'"{model}" is already added under this connection.')

        entry = {
            "id": _make_id(label or model, data["deployments"]),
            "connectionId": connection_id,
            "model": model,
            "label": label or None,
            "enabled": enabled,
            "notes": notes or "",
            "discovered": dict(discovered or {}),
            "overrides": dict(overrides or {}),
            "createdAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
        }
        data["deployments"].append(entry)
        _save(data)
        return hydrate(entry)


def add_deployments(connection_id: str, models: list[Any]) -> dict[str, list[Any]]:
    """Several at once, reporting each failure rather than stopping.

    One bad name among fifteen should not lose the other fourteen — the same
    shape the old `add_models` had, kept for the same reason.
    """
    added, failed = [], []
    for item in models or []:
        spec = {"model": item} if isinstance(item, str) else dict(item)
        try:
            discovered = {k: v for k, v in spec.items()
                          if k in ("context_tokens", "capabilities", "label", "provider")
                          and v is not None}
            added.append(add_deployment(connection_id=connection_id, model=spec["model"],
                                        label=spec.get("label"), discovered=discovered))
        except Exception as err:  # noqa: BLE001
            failed.append({"model": spec.get("model"),
                           "error": str(err) or "Could not add this model."})
    return {"added": added, "failed": failed}


def update_deployment(deployment_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        data = _load()
        index = next((i for i, e in enumerate(data["deployments"])
                      if e.get("id") == deployment_id), -1)
        if index == -1:
            raise KeyError(f"Unknown deployment: {deployment_id}")
        entry = dict(data["deployments"][index])
        for key in PATCH_KEYS:
            if key in patch:
                entry[key] = patch[key]
        data["deployments"][index] = entry
        _save(data)
        return hydrate(entry)


def record_discovery(deployment_id: str, facts: dict[str, Any]) -> dict[str, Any] | None:
    """Merge in what the provider has just told us about this model.

    Deliberately NOT reachable through `update_deployment`: `discovered` is what
    a provider said, `overrides` is what a person said, and a single patch
    surface that could write either would let a screen quietly overwrite the
    first with the second. They are separate fields because they answer to
    different authorities, and the merge order between them only means anything
    while that stays true.

    Returns None for an id that no longer exists, since discovery runs against a
    roster that can change underneath it.
    """
    with _lock:
        data = _load()
        index = next((i for i, e in enumerate(data["deployments"])
                      if e.get("id") == deployment_id), -1)
        if index == -1:
            return None
        entry = dict(data["deployments"][index])
        entry["discovered"] = {**(entry.get("discovered") or {}), **(facts or {})}
        data["deployments"][index] = entry
        _save(data)
        return hydrate(entry)


def delete_deployment(deployment_id: str) -> None:
    """Remove it, and everything learned about it.

    A later deployment can reuse this id — `_make_id` only avoids ids currently
    taken — so leaving a cooldown record or a refused-parameter record behind
    would hand somebody else's history to a model that has never been called.
    """
    from . import availability, effort, slots

    entry = next((e for e in _load()["deployments"] if e.get("id") == deployment_id), None)
    with _lock:
        data = _load()
        data["deployments"] = [e for e in data["deployments"] if e.get("id") != deployment_id]
        _save(data)

    availability.clear(deployment_id)
    slots.forget_deployment(deployment_id)
    if entry:
        hydrated = hydrate(entry)
        effort.clear(hydrated["version"].provider, entry.get("model"))


def delete_connection(connection_id: str) -> int:
    """Remove a connection and every deployment on it. Returns how many went.

    Cascading is the caller-facing behaviour; `connections.remove_connection`
    deliberately does not do it, so a caller has to choose.
    """
    from . import connections as connection_store

    doomed = [e.get("id") for e in _load()["deployments"]
              if e.get("connectionId") == connection_id]
    for deployment_id in doomed:
        delete_deployment(str(deployment_id))
    connection_store.remove_connection(connection_id)
    return len(doomed)


def is_ready(entry: dict[str, Any]) -> bool:
    """Whether this deployment has whatever credential it actually needs.

    The connection's stored `keyRequired` fact decides. Unlike the old row there
    is no host-regex fallback: a connection saved without that fact is one the
    probe never confirmed, and guessing from the URL is what let a gateway save
    as keyless and then reject every real turn.
    """
    key_required = entry.get("keyRequired")
    if not isinstance(key_required, bool):
        # Not established. Treat a stored secret as the answer, since a
        # connection carrying one was given it for a reason.
        return bool(entry.get("secretRef") and get_secret(entry["secretRef"]))
    if not key_required:
        return True
    ref = entry.get("secretRef")
    return bool(ref and get_secret(ref))


def version_of(entry: dict[str, Any]) -> Version:
    """The catalog's answer for a hydrated deployment."""
    return entry["version"]
