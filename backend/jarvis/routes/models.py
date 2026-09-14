"""Models and connections.

A secret is never in a response, on any route here. `secretRef` is stripped and
replaced by `hasSecret`: a screen needs to know whether a key is set, never what
it is. A secret goes IN (adding a connection, changing a key) and never comes
back out.

The logic behind adding, probing and discovering lives in `gateway/setup.py`, so
it can be exercised without HTTP; these routes are the surface over it.

**Rechecking every model is rate-limited on purpose.** The original fired every
enabled model's test simultaneously and was confirmed live to have mass-banned a
real roster — ten Gemini models all stamped unreachable inside one 150ms window,
seven of them working the moment each was retried alone — and to burn half a
day of a free tier in one click. Three at a time, and a preview route that says
what a full check will cost before anyone presses it.
"""

from __future__ import annotations

from typing import Any

import logging
import threading

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from ..gateway import availability, connections, deployments, providers, setup
from ..gateway.error_kind import availability_state_for
from ..gateway.probe import probe_endpoint

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/models")
#: Connections are their own noun, and the original serves them under their own
#: path even though they are read back through /api/models.
connections_router = APIRouter(prefix="/api/connections")

#: How many model tests may be in flight at once. See this module's own note.
RECHECK_CONCURRENCY = 3


#: The public shape of a model and of a connection, declared rather than
#: inherited from whatever the store happens to hold.
#:
#: These used to be `{**entry}` minus the secret, which made the internal record
#: the API: a field added to storage appeared on the wire unannounced, a field
#: renamed there changed the contract silently, and a stray key left in
#: `models.json` by an older build was served to the browser as though it meant
#: something. The front end could not catch any of it either — every one of its
#: model interfaces ends in an index signature that accepts any extra key.
#:
#: Listing the fields here does not stop the shape changing. It makes changing
#: it an edit to a named thing, which `tests/test_models_contract.py` then fails
#: on. `secretRef` is absent by construction rather than by subtraction.
MODEL_FIELDS = (
    "id", "model", "connectionId", "enabled", "notes", "adapter", "baseUrl",
    "keyRequired", "kind", "connectionProvider", "connectionLabel",
)

#: Four keys the flat model row served and this one does not: `caps`, `tier`,
#: `tags` and `billing`. Every one of them was produced by matching regular
#: expressions against the model's NAME and then served to the browser as
#: though it were a fact — which is how a paid-tier model wore a "free" badge
#: and how everything with "mini" inside it, Gemini Pro included, was ranked
#: cheap and fast. What a model can do now travels under `version`, with three
#: states instead of two and with per-field provenance saying which parts were
#: matched and which were observed.
#:
#: Nothing in the front end read any of the four. `test_models_contract.py`
#: measured that before the rebuild started, which is what made removing them
#: a decision rather than a gamble.
REMOVED_MODEL_FIELDS = ("caps", "tier", "tags", "billing", "provider")

CONNECTION_FIELDS = (
    "id", "label", "adapter", "baseUrl", "provider", "kind", "keyRequired", "createdAt",
)


def _select(source: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    """The declared fields that are actually present.

    Present-only rather than filled with `None`: a key absent from an older
    stored record stays absent, so this swap changes nothing for any record the
    app already holds. The only behaviour it removes is a key nobody declared
    reaching the wire.
    """
    return {name: source[name] for name in fields if name in source}


def _public_model(entry: dict[str, Any]) -> dict[str, Any]:
    """One deployment: what the user made, plus what the catalog says it is.

    `label` falls back to the model id here rather than being stored that way.
    The old row defaulted `label` to the model name at write time, so a row
    nobody had named was indistinguishable from one somebody had named after
    itself — and renaming the model later left the old name behind as though it
    had been chosen.
    """
    version = entry.get("version")
    return {**_select(entry, MODEL_FIELDS),
            "label": entry.get("label") or entry.get("model"),
            "version": version.as_dict() if version is not None else None,
            "hasSecret": bool(entry.get("secretRef")),
            "ready": deployments.is_ready(entry)}


def _public_connection(connection: dict[str, Any], models: list[dict[str, Any]]) -> dict[str, Any]:
    """A connection saved before the provider catalogue existed has no
    provider/kind of its own; it is backfilled at READ time, never migrated —
    the same read-time pattern `deployments.hydrate()` already uses."""
    backfill = ({} if connection.get("provider")
                else providers.provider_for_legacy(connection.get("adapter"),
                                                   connection.get("baseUrl")))
    return {
        **_select(connection, CONNECTION_FIELDS), **backfill,
        "hasSecret": bool(connection.get("secretRef")),
        "modelCount": sum(1 for m in models if m.get("connectionId") == connection.get("id")),
    }


def _health(models: list[dict[str, Any]]) -> dict[str, Any]:
    """Which models are being skipped right now, and for how long.

    A flat map keyed by model id, as the original serves it. The fields come
    from this build's own availability record — `state` is what it calls the
    original's `kind`, and `detail` its `reason` — rather than inventing a
    second vocabulary for the same fact.
    """
    out: dict[str, Any] = {}
    for entry in models:
        model_id = entry.get("id")
        if not model_id or availability.is_eligible(model_id):
            continue
        record = availability.status_of(model_id) or {}
        out[model_id] = {
            "reason": record.get("detail"),
            "kind": record.get("state"),
            "retryInMs": availability.retry_after_ms(model_id),
        }
    return out


@router.get("")
def listed() -> dict[str, Any]:
    models = deployments.list_deployments()
    return {
        "connections": [_public_connection(c, models) for c in connections.list_connections()],
        "models": [_public_model(m) for m in models],
        "health": _health(models),
    }


#: One node of the browse tree. Declared for the same reason the row above is:
#: a screen is written against these names, and a rename that compiles on both
#: sides arrives in the browser as wrong rendering rather than as a failure.
CATALOG_PROVIDER_KEYS = ("id", "label", "families")
CATALOG_FAMILY_KEYS = ("id", "label", "versions")
CATALOG_VERSION_KEYS = ("model", "label", "version", "deployments")


def _catalog_deployment(entry: dict[str, Any]) -> dict[str, Any]:
    """How one version is actually reachable — the crossing axis, made visible.

    The same model offered by two connections is ONE version with two routes,
    not two unrelated rows. That is the whole reason this tree exists: the flat
    list could not say it, so a person looking at their own roster could not
    tell a duplicate from a genuine second route with its own key and its own
    rate limit.
    """
    return {
        "id": entry["id"],
        "label": entry.get("label") or entry.get("model"),
        "connectionId": entry.get("connectionId"),
        "connectionLabel": entry.get("connectionLabel"),
        "enabled": bool(entry.get("enabled", True)),
        "ready": deployments.is_ready(entry),
    }


@router.get("/catalog")
def catalog() -> dict[str, Any]:
    """The roster as provider -> family -> version, for browsing.

    Built from the deployments this install actually has, never from a shipped
    model list. A catalog route that enumerated models Jarvis knows about would
    be a hardcoded roster wearing a hat, and it would go stale the week after
    it was written.

    Grouping is by the VERSION's provider — who makes the model — not by the
    connection it is reached through, so a model resold by a gateway appears
    under its maker beside the same maker's models reached directly. `unknown`
    is a real group and is sorted last: a local or unlisted model belongs
    somewhere a person can find it, not nowhere.
    """
    by_provider: dict[str, dict[str, Any]] = {}
    for entry in deployments.list_deployments():
        version = deployments.version_of(entry)
        if version is None:
            continue
        provider = by_provider.setdefault(version.provider, {"id": version.provider,
                                                             "label": version.provider,
                                                             "families": {}})
        # A version with no family is its own group, keyed by the model id, so
        # it is listed rather than silently dropped for not matching a pattern.
        family_id = version.family or version.model
        family = provider["families"].setdefault(
            family_id, {"id": family_id, "label": version.family or version.model,
                        "versions": {}})
        node = family["versions"].setdefault(
            version.model,
            {"model": version.model, "label": version.label,
             "version": version.as_dict(), "deployments": []})
        node["deployments"].append(_catalog_deployment(entry))

    def sorted_provider(row: dict[str, Any]) -> dict[str, Any]:
        families = [
            {**family, "versions": sorted(family["versions"].values(), key=lambda v: v["model"])}
            for family in sorted(row["families"].values(), key=lambda f: f["label"])
        ]
        return {**row, "families": families}

    ordered = sorted(by_provider.values(), key=lambda p: (p["id"] == "unknown", p["id"]))
    return {"providers": [sorted_provider(row) for row in ordered]}


@router.get("/providers")
def provider_tiles() -> dict[str, Any]:
    """The five user-facing tiles the "add a model" flow shows.

    Data only, and deliberately not adapter names: which wire format a provider
    resolves to stays server-side. See `gateway/providers.py`.
    """
    return {"providers": providers.PROVIDERS}


# --- adding, changing, removing --------------------------------------------

@connections_router.post("")
def add(body: dict[str, Any] = Body(default_factory=dict)):
    if not body.get("provider") and not body.get("adapter"):
        return JSONResponse({"ok": False, "error": "Please choose a provider."}, status_code=400)
    try:
        result = setup.create_connection_with_models(
            provider=body.get("provider"), adapter=body.get("adapter"),
            base_url=body.get("baseUrl"), label=body.get("label"), secret=body.get("secret"),
            models=body.get("models"), resolved=body.get("resolved"))
    except (ValueError, KeyError) as err:
        return JSONResponse({"ok": False, "error": str(err) or "Could not add that connection."},
                            status_code=400)
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)

    models = deployments.list_deployments()
    return {"ok": True, "connection": _public_connection(result["connection"], models),
            "added": [_public_model(m) for m in result["added"]],
            "failed": result["failed"], "steps": result.get("steps")}


@connections_router.post("/probe")
def probe(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    """Try an address whose wire format is not known yet.

    Answers with every attempt it made, in plain language, so a failure can be
    explained rather than reduced to one generic sentence — the whole reason
    this exists. A 401 with no key supplied is "reached it, needs a key", never
    "that key is invalid".
    """
    result = probe_endpoint(str(body.get("baseUrl") or ""), body.get("secret"))
    return {"ok": result.ok, "steps": result.steps, "adapter": result.adapter,
            "baseUrl": result.base_url, "kind": result.kind,
            "keyRequired": result.key_required, "models": result.models,
            "error": result.error, "needsKey": result.needs_key}


@connections_router.post("/discover")
def discover(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return setup.discover_models(
        adapter=body.get("adapter") or "openai-compatible", base_url=body.get("baseUrl"),
        secret=body.get("secret"), connection_id=body.get("connectionId"))


@connections_router.patch("/{connection_id}")
def edit_connection(connection_id: str, body: dict[str, Any] = Body(default_factory=dict)):
    patch = {k: body[k] for k in ("label", "baseUrl", "secret") if k in body}
    try:
        conn = connections.update_connection(connection_id, patch)
    except KeyError:
        return JSONResponse({"ok": False, "error": "Unknown connection."}, status_code=404)
    except ValueError as err:
        return JSONResponse({"ok": False, "error": str(err)}, status_code=400)
    return {"ok": True, "connection": _public_connection(conn, deployments.list_deployments())}


@connections_router.delete("/{connection_id}")
def remove_connection(connection_id: str) -> dict[str, Any]:
    """Removing a connection removes the models that hung off it — they cannot
    answer without it. The count is reported so the screen can say so."""
    # `delete_connection` cascades and removes the connection itself, so there
    # is no second call here. There used to be one, and it was harmless only
    # because removing an already-removed connection is a no-op.
    removed = deployments.delete_connection(connection_id)
    return {"ok": True, "removedModels": removed}


@router.post("")
def add_models(body: dict[str, Any] = Body(default_factory=dict)):
    """More models under a connection that already works.

    No test: the address and key were validated when the connection was added,
    and re-testing here would mean one live API call per model.
    """
    connection_id = body.get("connectionId")
    models = body.get("models")
    if not connection_id or not isinstance(models, list) or not models:
        return JSONResponse({"ok": False, "error": "Pick a connection and at least one model."},
                            status_code=400)
    try:
        result = deployments.add_deployments(connection_id, models)
    except KeyError:
        return JSONResponse({"ok": False, "error": "Unknown connection."}, status_code=404)
    return {"ok": True, "added": [_public_model(m) for m in result["added"]],
            "failed": result["failed"]}


@router.patch("/{model_id}")
def edit_model(model_id: str, body: dict[str, Any] = Body(default_factory=dict)):
    try:
        entry = deployments.update_deployment(model_id, body or {})
    except KeyError:
        return JSONResponse({"ok": False, "error": "Unknown model."}, status_code=404)
    return {"ok": True, "model": _public_model(entry)}


@router.delete("/{model_id}")
def remove_model(model_id: str) -> dict[str, Any]:
    deployments.delete_deployment(model_id)
    return {"ok": True}


# --- is it actually working ------------------------------------------------

def _test_and_record(entry: dict[str, Any]) -> dict[str, Any]:
    """Test one model and let the result update its availability badge.

    Bookkeeping must never turn a working test into a failed request, so a
    failure to RECORD is logged and swallowed — the test's own answer stands.
    """
    from ..adapters import get_adapter

    state = "unreachable"
    try:
        result = get_adapter(entry.get("adapter")).test_connection(entry)
    except Exception as err:  # noqa: BLE001 — an unreachable model is a result
        # The RAW error classifies more accurately than the sentence an adapter
        # already reduced it to: rate-limited and misconfigured are different
        # states with different retry behaviour.
        state = availability_state_for(err)
        result = {"ok": False, "error": str(err) or "That model could not be reached."}

    try:
        if result.get("ok"):
            availability.clear(entry["id"])
        else:
            availability.record(entry["id"], state, detail=result.get("error"))
    except Exception:  # noqa: BLE001
        logger.exception("could not record availability for %s", entry.get("id"))
    return result


@router.post("/{model_id}/test")
def test_model(model_id: str):
    entry = deployments.get_deployment(model_id)
    if entry is None:
        return JSONResponse({"ok": False, "error": "Unknown model."}, status_code=404)
    return _test_and_record(entry)


@router.get("/recheck/preview")
def recheck_preview() -> dict[str, Any]:
    """What checking everything would cost, before anyone presses it.

    Read-only — it makes no model calls. It only reports what each provider has
    already told us about the credit left, where a provider reports that at all.
    """
    from ..cost import store as cost_store

    enabled = [e for e in deployments.list_deployments() if e.get("enabled")]
    not_working = [e for e in enabled if not availability.is_eligible(e["id"])]

    by_connection = []
    for conn in connections.list_connections():
        count = sum(1 for e in enabled if e.get("connectionId") == conn.get("id"))
        if not count:
            continue
        balance = cost_store.get_balance(str(conn.get("provider") or "")) or {}
        by_connection.append({"id": conn.get("id"), "label": conn.get("label"), "count": count,
                              "isFreeTier": balance.get("isFreeTier"),
                              "remaining": balance.get("remaining")})
    return {"total": len(enabled), "notWorking": len(not_working), "byConnection": by_connection}


@router.post("/recheck")
def recheck(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    """`scope: "all"` checks every enabled model; anything else checks only the
    ones not currently working — the cheap default, since a model already
    answering needs no proof."""
    enabled = [e for e in deployments.list_deployments() if e.get("enabled")]
    entries = (enabled if body.get("scope") == "all"
               else [e for e in enabled if not availability.is_eligible(e["id"])])

    # A simple pull-based pool rather than a library: this is the only place in
    # the app that needs one, and firing them all at once is what banned a real
    # roster (see this module's own note).
    queue = list(entries)
    lock = threading.Lock()

    def lane() -> None:
        while True:
            with lock:
                if not queue:
                    return
                entry = queue.pop(0)
            _test_and_record(entry)

    lanes = [threading.Thread(target=lane, daemon=True)
             for _ in range(min(RECHECK_CONCURRENCY, len(entries)))]
    for thread in lanes:
        thread.start()
    for thread in lanes:
        thread.join()

    return {"ok": True, "models": [_public_model(m) for m in deployments.list_deployments()]}
