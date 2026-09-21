"""Connections and the models listed under them — rows, and nothing but rows.

A leaf on purpose: it imports the database and the clock and nothing else, so
anything can read it without a cycle. What the rows *mean* — whether a selection
is available, what a provider can do — is worked out elsewhere.

Credentials are never in here. A connection holds only the *name* of its secret
(`secret_ref`); the key itself lives in `.env` through `config.save_secret`.
"""

from __future__ import annotations

import functools
import json
import secrets
import threading
from dataclasses import dataclass
from typing import Any, Callable

from ..db import get_db
from ..jscompat import now_iso

#: Every read and write here goes through the ONE shared database connection, and
#: uvicorn runs sync routes on a pool of threads. Two of them in this module at once
#: is how a shared connection returns the wrong row to the wrong caller, or fails with
#: "bad parameter or other API misuse" — found by hammering these routes from several
#: threads. So they take turns, as the other stores over that connection do.
_lock = threading.RLock()


def _locked(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def inner(*args: Any, **kwargs: Any) -> Any:
        with _lock:
            return fn(*args, **kwargs)

    return inner


@dataclass(frozen=True)
class Connection:
    id: str
    kind: str
    format: str
    label: str
    base_url: str | None
    secret_ref: str | None
    created_at: str
    checked_at: str | None
    #: When a discovery last SUCCEEDED, which is what "no longer listed" is judged against.
    discovered_at: str | None
    #: 'untested' | 'ok' | 'error'. Set by a connection test and by nothing else:
    #: a discovery or a turn going wrong says something about that request, not
    #: about whether the connection itself is good.
    state: str
    detail: str | None


@dataclass(frozen=True)
class Model:
    provider_id: str
    #: The provider's own identifier, verbatim — exactly what is sent on the wire.
    model_id: str
    label: str | None
    #: 'discovered' | 'manual'
    source: str
    #: What the provider reported that a request needs (see `types.Discovered`).
    facts: dict[str, Any] | None
    added_at: str
    #: The last discovery that listed it. Older than the connection's
    #: `discovered_at` means the provider has stopped listing it — which is a
    #: note, not a verdict: the model stays selectable.
    last_seen_at: str | None


def _connection(row: Any) -> Connection:
    return Connection(
        id=row["id"], kind=row["kind"], format=row["format"], label=row["label"],
        base_url=row["base_url"], secret_ref=row["secret_ref"], created_at=row["created_at"],
        checked_at=row["checked_at"], discovered_at=row["discovered_at"],
        state=row["state"], detail=row["detail"],
    )


def _model(row: Any) -> Model:
    try:
        facts = json.loads(row["facts_json"]) if row["facts_json"] else None
    except ValueError:
        facts = None
    return Model(provider_id=row["provider_id"], model_id=row["model_id"], label=row["label"],
                 source=row["source"], facts=facts if isinstance(facts, dict) else None,
                 added_at=row["added_at"], last_seen_at=row["last_seen_at"])


# --- connections ---------------------------------------------------------------------

@_locked
def list_connections() -> list[Connection]:
    rows = get_db().execute("SELECT * FROM model_providers ORDER BY created_at, id").fetchall()
    return [_connection(r) for r in rows]


@_locked
def get_connection(connection_id: str) -> Connection | None:
    row = get_db().execute("SELECT * FROM model_providers WHERE id = ?", (connection_id,)).fetchone()
    return _connection(row) if row else None


def new_id() -> str:
    return f"prov_{secrets.token_hex(6)}"


@_locked
def add_connection(*, connection_id: str, kind: str, format: str, label: str,
                   base_url: str | None, secret_ref: str | None) -> Connection:
    get_db().execute(
        "INSERT INTO model_providers (id, kind, format, label, base_url, secret_ref, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (connection_id, kind, format, label, base_url, secret_ref, now_iso()),
    )
    found = get_connection(connection_id)
    assert found is not None
    return found


#: The only fields an edit may change. The kind and the format are what the
#: connection IS; changing them is deleting it and adding another.
_EDITABLE = {"label", "base_url", "secret_ref"}


@_locked
def update_connection(connection_id: str, **fields: Any) -> Connection | None:
    unknown = set(fields) - _EDITABLE
    if unknown:
        raise ValueError(f"a connection's {', '.join(sorted(unknown))} cannot be edited")
    if fields:
        columns = ", ".join(f"{name} = ?" for name in fields)
        get_db().execute(f"UPDATE model_providers SET {columns} WHERE id = ?",
                         (*fields.values(), connection_id))
    return get_connection(connection_id)


@_locked
def record_check(connection_id: str, state: str, detail: str | None) -> None:
    get_db().execute(
        "UPDATE model_providers SET state = ?, detail = ?, checked_at = ? WHERE id = ?",
        (state, detail, now_iso(), connection_id),
    )


@_locked
def delete_connection(connection_id: str) -> bool:
    """Removes the connection and (by cascade) its models. It does NOT touch the
    selection preference: a selection pointing at a connection that no longer
    exists is left standing, and reported as exactly that."""
    cursor = get_db().execute("DELETE FROM model_providers WHERE id = ?", (connection_id,))
    return cursor.rowcount > 0


# --- models --------------------------------------------------------------------------

@_locked
def list_models(connection_id: str | None = None) -> list[Model]:
    if connection_id is None:
        rows = get_db().execute("SELECT * FROM provider_models ORDER BY provider_id, added_at, model_id").fetchall()
    else:
        rows = get_db().execute(
            "SELECT * FROM provider_models WHERE provider_id = ? ORDER BY added_at, model_id",
            (connection_id,)).fetchall()
    return [_model(r) for r in rows]


@_locked
def get_model(connection_id: str, model_id: str) -> Model | None:
    row = get_db().execute(
        "SELECT * FROM provider_models WHERE provider_id = ? AND model_id = ?",
        (connection_id, model_id)).fetchone()
    return _model(row) if row else None


@_locked
def add_manual_model(connection_id: str, model_id: str, label: str | None = None) -> Model:
    """A model typed in by hand. Already listed is fine — it is returned as it is,
    so adding the same id twice, or one discovery already found, changes nothing."""
    existing = get_model(connection_id, model_id)
    if existing:
        return existing
    get_db().execute(
        "INSERT INTO provider_models (provider_id, model_id, label, source, facts_json, added_at, last_seen_at) "
        "VALUES (?, ?, ?, 'manual', NULL, ?, NULL)",
        (connection_id, model_id, label, now_iso()),
    )
    found = get_model(connection_id, model_id)
    assert found is not None
    return found


@_locked
def remove_model(connection_id: str, model_id: str) -> bool:
    cursor = get_db().execute(
        "DELETE FROM provider_models WHERE provider_id = ? AND model_id = ?", (connection_id, model_id))
    return cursor.rowcount > 0


@_locked
def record_discovery(connection_id: str, discovered: list[Any]) -> dict[str, int]:
    """Fold a successful discovery into the list.

    Adds what is new, refreshes what is listed again, and REMOVES NOTHING: a model
    the provider no longer lists keeps its row and stays usable. A provider's list
    is not the last word on what it will run — it omits unreleased and private
    models and reorganises its catalogue — so its silence is not grounds to take a
    model away from someone who chose it.
    """
    db = get_db()
    now = now_iso()
    added = updated = 0
    db.execute("BEGIN")
    try:
        for item in discovered:
            facts = json.dumps(item.facts) if item.facts else None
            if get_model(connection_id, item.model_id):
                db.execute(
                    "UPDATE provider_models SET label = COALESCE(?, label), facts_json = ?, "
                    "source = 'discovered', last_seen_at = ? WHERE provider_id = ? AND model_id = ?",
                    (item.label, facts, now, connection_id, item.model_id))
                updated += 1
            else:
                db.execute(
                    "INSERT INTO provider_models (provider_id, model_id, label, source, facts_json, added_at, last_seen_at) "
                    "VALUES (?, ?, ?, 'discovered', ?, ?, ?)",
                    (connection_id, item.model_id, item.label, facts, now, now))
                added += 1
        db.execute("UPDATE model_providers SET discovered_at = ? WHERE id = ?", (now, connection_id))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return {"added": added, "updated": updated}
