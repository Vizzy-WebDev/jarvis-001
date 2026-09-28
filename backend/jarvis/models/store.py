"""Connections and the models listed under them — rows, and nothing but rows.

A leaf on purpose: it imports the database and the clock and nothing else, so
anything can read it without a cycle. What the rows *mean* — whether a selection
is available, what a provider can do, how long a failure holds a model back — is
worked out elsewhere.

Three kinds of fact about a model, kept in three places (migration 35):

* the **catalog** (`provider_catalog`) — what the provider reported. Only a refresh
  or a hand-added id writes it;
* the **policy** (`provider_policy`) — what the person decided (removed, their own
  label). A refresh never touches it;
* the **runtime** — how calls went (`model_outcomes`) and what is being held back
  for now (`model_holds`).

A model is listed when it is in the catalog and the person hasn't removed it.

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
    #: The credential (key/account) this connection's requests go out on. One key per
    #: connection today, so it is the connection's own id unless stated otherwise — but
    #: a failure that is about the credential is held against THIS, not the connection.
    credential_id: str = ""
    #: The gateway this connection says it is ('openrouter', 'omniroute'), whose own
    #: listing fields are then read by `models/gateways/`. None: a plain server.
    gateway_kind: str | None = None
    #: Auto prefers this connection's router models over walking its others one by one.
    prefer_routers: bool = False


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
    #: The person's own ordering, where they gave one (policy). Nothing sets it yet.
    user_order: int | None = None


def _connection(row: Any) -> Connection:
    return Connection(
        id=row["id"], kind=row["kind"], format=row["format"], label=row["label"],
        base_url=row["base_url"], secret_ref=row["secret_ref"], created_at=row["created_at"],
        checked_at=row["checked_at"], discovered_at=row["discovered_at"],
        state=row["state"], detail=row["detail"],
        credential_id=row["credential_id"] or row["id"], gateway_kind=row["gateway_kind"] or None,
        prefer_routers=bool(row["prefer_routers"]),
    )


def _facts(text: str | None) -> dict[str, Any] | None:
    try:
        facts = json.loads(text) if text else None
    except ValueError:
        facts = None
    return facts if isinstance(facts, dict) else None


def _model(row: Any) -> Model:
    return Model(provider_id=row["provider_id"], model_id=row["model_id"], label=row["label"],
                 source=row["source"], facts=_facts(row["facts_json"]),
                 added_at=row["added_at"], last_seen_at=row["last_seen_at"], user_order=row["user_order"])


#: A model as the person sees it: the catalog row, with the person's own decisions
#: laid over it. No policy row means nothing was decided — listed, and enabled.
_LISTED = ("SELECT c.provider_id, c.model_id, COALESCE(p.user_label, c.label) AS label, c.source, "
           "c.facts_json, c.added_at, c.last_seen_at, p.user_order AS user_order "
           "FROM provider_catalog c LEFT JOIN provider_policy p "
           "ON p.provider_id = c.provider_id AND p.model_id = c.model_id "
           "WHERE COALESCE(p.excluded, 0) = 0 AND COALESCE(p.enabled, 1) = 1")


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
                   base_url: str | None, secret_ref: str | None, gateway_kind: str | None = None,
                   prefer_routers: bool = False) -> Connection:
    get_db().execute(
        "INSERT INTO model_providers (id, kind, format, label, base_url, secret_ref, created_at, "
        "gateway_kind, prefer_routers) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (connection_id, kind, format, label, base_url, secret_ref, now_iso(), gateway_kind,
         1 if prefer_routers else 0),
    )
    found = get_connection(connection_id)
    assert found is not None
    return found


#: The only fields an edit may change. The kind and the format are what the
#: connection IS; changing them is deleting it and adding another.
_EDITABLE = {"label", "base_url", "secret_ref", "gateway_kind", "prefer_routers"}


@_locked
def update_connection(connection_id: str, **fields: Any) -> Connection | None:
    unknown = set(fields) - _EDITABLE
    if unknown:
        raise ValueError(f"a connection's {', '.join(sorted(unknown))} cannot be edited")
    if "prefer_routers" in fields:
        fields["prefer_routers"] = 1 if fields["prefer_routers"] else 0
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
    """Every model the person has on their lists (not removed, not disabled)."""
    if connection_id is None:
        rows = get_db().execute(_LISTED + " ORDER BY c.provider_id, c.added_at, c.model_id").fetchall()
    else:
        rows = get_db().execute(_LISTED + " AND c.provider_id = ? ORDER BY c.added_at, c.model_id",
                                (connection_id,)).fetchall()
    return [_model(r) for r in rows]


@_locked
def get_model(connection_id: str, model_id: str) -> Model | None:
    """A listed model, or None — including for one the person removed."""
    row = get_db().execute(_LISTED + " AND c.provider_id = ? AND c.model_id = ?",
                           (connection_id, model_id)).fetchone()
    return _model(row) if row else None


def _in_catalog(connection_id: str, model_id: str) -> Any:
    return get_db().execute(
        "SELECT facts_json FROM provider_catalog WHERE provider_id = ? AND model_id = ?",
        (connection_id, model_id)).fetchone()


def _set_policy(connection_id: str, model_id: str, **fields: Any) -> None:
    columns = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    updates = ", ".join(f"{name} = excluded.{name}" for name in fields)
    get_db().execute(
        f"INSERT INTO provider_policy (provider_id, model_id, {columns}) VALUES (?, ?, {marks}) "
        f"ON CONFLICT(provider_id, model_id) DO UPDATE SET {updates}",
        (connection_id, model_id, *fields.values()))


@_locked
def add_manual_model(connection_id: str, model_id: str, label: str | None = None) -> Model:
    """A model typed in by hand. Already listed is fine — it is returned as it is,
    so adding the same id twice, or one discovery already found, changes nothing.

    One the person had REMOVED comes back: adding it is their own decision, made
    again, and the only thing that ever lifts a removal."""
    existing = get_model(connection_id, model_id)
    if existing:
        return existing
    if not _in_catalog(connection_id, model_id):
        get_db().execute(
            "INSERT INTO provider_catalog (provider_id, model_id, label, source, facts_json, added_at, last_seen_at) "
            "VALUES (?, ?, NULL, 'manual', NULL, ?, NULL)",
            (connection_id, model_id, now_iso()),
        )
    _set_policy(connection_id, model_id, excluded=0, **({"user_label": label} if label else {}))
    found = get_model(connection_id, model_id)
    assert found is not None
    return found


@_locked
def exclude_model(connection_id: str, model_id: str) -> bool:
    """Take a model off the person's list, for good: a refresh that lists it again
    does not bring it back (it writes only the catalog). False if it wasn't listed."""
    if get_model(connection_id, model_id) is None:
        return False
    _set_policy(connection_id, model_id, excluded=1)
    return True


def _merged(old: str | None, new: dict[str, Any] | None) -> str | None:
    """Facts after a refresh: every key the provider reported this time replaces its
    old value, and a key it did not mention is KEPT — a thinner answer is not a
    retraction. (A provider retracts by reporting the key as False.)"""
    facts = {**(_facts(old) or {}), **(new or {})}
    return json.dumps(facts) if facts else None


@_locked
def record_discovery(connection_id: str, discovered: list[Any]) -> dict[str, int]:
    """Fold a successful discovery into the CATALOG — and nothing else.

    Adds what is new, refreshes what is listed again, and REMOVES NOTHING: a model
    the provider no longer lists keeps its row and stays usable. A provider's list
    is not the last word on what it will run — it omits unreleased and private
    models and reorganises its catalogue — so its silence is not grounds to take a
    model away from someone who chose it. The person's own decisions (policy) are
    never touched, so a model they removed stays removed.
    """
    db = get_db()
    now = now_iso()
    added = updated = 0
    db.execute("BEGIN")
    try:
        for item in discovered:
            row = _in_catalog(connection_id, item.model_id)
            if row:
                db.execute(
                    "UPDATE provider_catalog SET label = COALESCE(?, label), facts_json = ?, "
                    "source = 'discovered', last_seen_at = ? WHERE provider_id = ? AND model_id = ?",
                    (item.label, _merged(row["facts_json"], item.facts), now, connection_id, item.model_id))
                updated += 1
            else:
                db.execute(
                    "INSERT INTO provider_catalog (provider_id, model_id, label, source, facts_json, added_at, "
                    "last_seen_at) VALUES (?, ?, ?, 'discovered', ?, ?, ?)",
                    (connection_id, item.model_id, item.label, _merged(None, item.facts), now, now))
                added += 1
        db.execute("UPDATE model_providers SET discovered_at = ? WHERE id = ?", (now, connection_id))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return {"added": added, "updated": updated}


# --- outcomes ------------------------------------------------------------------------

@dataclass(frozen=True)
class Outcome:
    """What last happened when this model was really called. A record, not a verdict."""

    provider_id: str
    model_id: str
    last_ok_at: str | None
    last_fail_at: str | None
    fail_kind: str | None
    fail_status: int | None
    fail_message: str | None
    #: Moving average of how long it took to start answering, in milliseconds.
    ttft_ms: int | None = None
    #: Failures since it last answered.
    fail_streak: int = 0


@_locked
def record_success(connection_id: str, model_id: str, ttft_ms: int | None = None) -> None:
    get_db().execute(
        "INSERT INTO model_outcomes (provider_id, model_id, last_ok_at, ttft_ms, fail_streak) "
        "VALUES (?, ?, ?, ?, 0) "
        "ON CONFLICT(provider_id, model_id) DO UPDATE SET last_ok_at = excluded.last_ok_at, fail_streak = 0, "
        "ttft_ms = CASE WHEN excluded.ttft_ms IS NULL THEN model_outcomes.ttft_ms "
        "               WHEN model_outcomes.ttft_ms IS NULL THEN excluded.ttft_ms "
        "               ELSE CAST(ROUND(0.6 * model_outcomes.ttft_ms + 0.4 * excluded.ttft_ms) AS INTEGER) END",
        (connection_id, model_id, now_iso(), ttft_ms))


@_locked
def record_failure(connection_id: str, model_id: str, *, kind: str | None, status: int | None,
                   message: str | None) -> None:
    get_db().execute(
        "INSERT INTO model_outcomes (provider_id, model_id, last_fail_at, fail_kind, fail_status, fail_message, "
        "fail_streak) VALUES (?, ?, ?, ?, ?, ?, 1) "
        "ON CONFLICT(provider_id, model_id) DO UPDATE SET last_fail_at = excluded.last_fail_at, "
        "fail_kind = excluded.fail_kind, fail_status = excluded.fail_status, "
        "fail_message = excluded.fail_message, fail_streak = model_outcomes.fail_streak + 1",
        (connection_id, model_id, now_iso(), kind, status, (message or "")[:400]))


@_locked
def list_outcomes() -> dict[tuple[str, str], Outcome]:
    rows = get_db().execute("SELECT * FROM model_outcomes").fetchall()
    return {(r["provider_id"], r["model_id"]): Outcome(
        provider_id=r["provider_id"], model_id=r["model_id"], last_ok_at=r["last_ok_at"],
        last_fail_at=r["last_fail_at"], fail_kind=r["fail_kind"], fail_status=r["fail_status"],
        fail_message=r["fail_message"], ttft_ms=r["ttft_ms"], fail_streak=r["fail_streak"] or 0) for r in rows}


@_locked
def recent_answers(limit: int = 300) -> dict[str, str]:
    """`{model id: when it last answered}` from the saved replies themselves — the
    provider-reported id each assistant message was stored with. Real evidence a model
    worked here, available from before outcomes were recorded."""
    rows = get_db().execute(
        "SELECT m, MAX(t) AS t FROM ("
        "  SELECT json_extract(payload, '$.modelId') AS m, created_at AS t FROM ("
        "    SELECT payload, created_at FROM messages WHERE role = 'assistant' AND payload IS NOT NULL "
        "    ORDER BY id DESC LIMIT ?)) "
        "WHERE m IS NOT NULL GROUP BY m", (limit,)).fetchall()
    return {r["m"]: r["t"] for r in rows}


# --- holds ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Hold:
    """A failure that is holding something back until `until`. What it holds is
    `scope` + `hold_key`: one model on one credential, a credential, or a whole
    provider connection. What that MEANS for choosing a model is `health.py`'s."""

    scope: str
    hold_key: str
    connection_id: str
    credential_id: str
    model_id: str | None
    kind: str | None
    status: int | None
    message: str | None
    failed_at: str
    until: str
    streak: int
    retry_after_s: float | None


def _hold(row: Any) -> Hold:
    return Hold(scope=row["scope"], hold_key=row["hold_key"], connection_id=row["connection_id"],
                credential_id=row["credential_id"], model_id=row["model_id"], kind=row["kind"],
                status=row["status"], message=row["message"], failed_at=row["failed_at"], until=row["until"],
                streak=row["streak"], retry_after_s=row["retry_after_s"])


@_locked
def get_hold(scope: str, hold_key: str) -> Hold | None:
    row = get_db().execute("SELECT * FROM model_holds WHERE scope = ? AND hold_key = ?",
                           (scope, hold_key)).fetchone()
    return _hold(row) if row else None


@_locked
def put_hold(hold: Hold) -> None:
    get_db().execute(
        "INSERT OR REPLACE INTO model_holds (scope, hold_key, connection_id, credential_id, model_id, kind, "
        "status, message, failed_at, until, streak, retry_after_s) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (hold.scope, hold.hold_key, hold.connection_id, hold.credential_id, hold.model_id, hold.kind,
         hold.status, (hold.message or "")[:400], hold.failed_at, hold.until, hold.streak, hold.retry_after_s))


@_locked
def list_holds() -> list[Hold]:
    return [_hold(r) for r in get_db().execute("SELECT * FROM model_holds").fetchall()]


@_locked
def delete_holds(pairs: list[tuple[str, str]]) -> None:
    """Lift the holds named `(scope, hold_key)`."""
    for scope, key in pairs:
        get_db().execute("DELETE FROM model_holds WHERE scope = ? AND hold_key = ?", (scope, key))


@_locked
def delete_connection_holds(connection_id: str, scopes: tuple[str, ...]) -> None:
    marks = ", ".join("?" for _ in scopes)
    get_db().execute(f"DELETE FROM model_holds WHERE connection_id = ? AND scope IN ({marks})",
                     (connection_id, *scopes))
