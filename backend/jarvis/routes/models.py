"""Models and providers.

A secret is never in a response, on any route here. `credentialRef` never
leaves this module — a screen needs to know whether a key is set, never what
it is. A secret goes IN (adding a provider, changing a key) and never comes
back out.

The logic behind adding, probing and discovering lives in
`model_system/setup.py`, so it can be exercised without HTTP; these routes are
the surface over it. The wire shape here is the FRONTEND's contract
(`frontend/lib/api-types.ts`) and is kept stable across the model-system
rebuild on purpose — a "connection" on the wire is a `model_system.Provider`
underneath, and a "deployment" is a `model_system.ResolvedModel`, but neither
rename reaches the browser, so the shipped front end needed no changes.

**Rechecking every model is rate-limited on purpose.** Firing every enabled
model's test simultaneously has been confirmed live to mass-ban a real
roster — ten models all stamped unreachable inside one 150ms window, most of
them working the moment each was retried alone — and to burn half a day of a
free tier in one click. Three at a time, and a preview route that says what a
full check will cost before anyone presses it.
"""

from __future__ import annotations

from typing import Any

import logging
import threading

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from ..model_system import health, setup
from ..model_system.adapters import get_adapter
from ..model_system.credentials import CredentialStatus
from ..model_system.errors import ErrorKind, classify
from ..model_system.probe import probe_endpoint
from ..model_system.providers import list_providers, remove_provider, update_provider
from ..model_system.registry import delete_model, get_model, list_models, update_model

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/models")
#: Providers are their own noun, and the front end serves them under their own
#: path even though they are read back through /api/models — kept from the
#: pre-rebuild API, which called the same noun "connections".
connections_router = APIRouter(prefix="/api/connections")

#: How many model tests may be in flight at once. See this module's own note.
RECHECK_CONCURRENCY = 3

#: The five user-facing tiles the "add a model" flow shows — data only, zero
#: imports, exactly as recorded in `tests/contract/fixtures/0004-*.json` from
#: the original Node build. Deliberately its OWN frozen vocabulary rather than
#: derived from `model_system.providers.BUILTIN_TEMPLATES`: `adapter` and
#: `kind` here are cosmetic, historical labels a screen has always shown next
#: to a tile ("first-party" / "local", "openai-compatible" hyphenated) and
#: nothing downstream reads them to decide how to actually reach a provider —
#: that resolution happens server-side from the tile's `id` alone, in
#: `model_system.setup.create_provider_with_models`. Coupling this frozen
#: display shape to the model system's own internal enum values would make an
#: unrelated internal rename break a contract that has nothing to do with it.
_PROVIDER_TILES: tuple[dict[str, Any], ...] = (
    {
        "id": "openai", "label": "OpenAI", "icon": "🤖", "iconBg": "#10A37F",
        "adapter": "openai-compatible", "baseUrl": "https://api.openai.com/v1",
        "urlEditable": False, "keyRequired": True, "kind": "first-party",
        "suggestions": ["gpt-5.6-luna"], "keyHint": "Paste your OpenAI API key.",
    },
    {
        "id": "anthropic", "label": "Anthropic", "icon": "✳️", "iconBg": "#D97757",
        "adapter": "anthropic", "baseUrl": None,
        "urlEditable": False, "keyRequired": True, "kind": "first-party",
        "suggestions": ["claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5"],
        "keyHint": "Paste your Anthropic API key.",
    },
    {
        "id": "gemini", "label": "Gemini", "icon": "✨", "iconBg": "#4285F4",
        "adapter": "gemini", "baseUrl": None,
        "urlEditable": False, "keyRequired": True, "kind": "first-party",
        "suggestions": ["gemini-3.5-flash", "gemini-3.6-flash", "gemini-3-pro"],
        "keyHint": "Paste your Gemini API key.",
    },
    {
        "id": "local", "label": "Local server", "icon": "💻", "iconBg": "#4B5563",
        "adapter": "openai-compatible", "baseUrl": "http://localhost:11434/v1",
        "urlEditable": True, "keyRequired": False, "kind": "local",
        "suggestions": ["llama3.1", "mistral"],
        "keyHint": "Usually not needed for a local server.",
    },
    {
        "id": "custom", "label": "Custom", "icon": "🔧", "iconBg": "#6B7280",
        # Everything about a Custom row is resolved by probing the address.
        "adapter": None, "baseUrl": None,
        "urlEditable": True, "keyRequired": None, "kind": None,
        "suggestions": [],
        "keyHint": "Leave blank if the server needs no key — Jarvis will tell you if one is required.",
    },
)


def _version_of(model: Any) -> dict[str, Any]:
    """What the catalog says a model IS, in the frontend's own `ModelVersion`
    shape (`frontend/lib/api-types.ts`) — pre-dating this rebuild and kept
    stable on purpose. `ResolvedModel.as_dict()` is a different, newer shape
    (`model_system`'s own internal contract, e.g. for a future direct route);
    this is the one translation point between the two, so the two can drift
    without a screen silently receiving the wrong field names.
    """
    return {
        "provider": model.maker, "model": model.native_model_id,
        "label": model.display_name or model.native_model_id, "family": model.family,
        "pinned": model.pinned.value, "contextTokens": model.context_window,
        "capabilities": model.capabilities.as_dict(), "effort": model.reasoning.as_dict(),
        "quality": model.quality, "lifecycle": model.status,
        "provenance": dict(model.provenance),
    }


def _public_model(model: Any) -> dict[str, Any]:
    """One model made callable through one provider: what the user made, plus
    what the registry says it is."""
    provider = model.provider
    return {
        "id": model.id, "model": model.native_model_id,
        "connectionId": provider.id, "enabled": model.enabled, "notes": model.notes,
        "adapter": provider.adapter, "baseUrl": provider.base_url,
        "keyRequired": provider.key_required, "kind": provider.kind.value,
        "connectionProvider": provider.id, "connectionLabel": provider.label,
        "label": model.display_name or model.native_model_id,
        "version": _version_of(model),
        "hasSecret": provider.credential_status is not CredentialStatus.NOT_CONFIGURED,
        "ready": provider.credential_status is CredentialStatus.CONFIGURED,
    }


def _public_connection(provider: Any, models: list[Any]) -> dict[str, Any]:
    return {
        "id": provider.id, "label": provider.label, "adapter": provider.adapter,
        "baseUrl": provider.base_url, "provider": provider.id, "kind": provider.kind.value,
        "keyRequired": provider.key_required, "createdAt": provider.created_at,
        "hasSecret": provider.credential_status is not CredentialStatus.NOT_CONFIGURED,
        "modelCount": sum(1 for m in models if m.provider.id == provider.id),
    }


def _health(models: list[Any]) -> dict[str, Any]:
    """Which models are being skipped right now, and for how long. A flat map
    keyed by model id — a model nobody has had trouble with is simply
    absent."""
    out: dict[str, Any] = {}
    for model in models:
        if health.is_eligible(model.id):
            continue
        record = health.status_of(model.id) or {}
        out[model.id] = {"reason": record.get("detail"), "kind": record.get("state"),
                         "retryInMs": health.retry_after_ms(model.id)}
    return out


@router.get("")
def listed() -> dict[str, Any]:
    models = list_models()
    return {
        "connections": [_public_connection(p, models) for p in list_providers()],
        "models": [_public_model(m) for m in models],
        "health": _health(models),
    }


def _catalog_route(model: Any) -> dict[str, Any]:
    """How one version is actually reachable — the crossing axis, made
    visible. The same model offered by two providers is ONE version with two
    routes, not two unrelated rows."""
    return {
        "id": model.id, "label": model.display_name or model.native_model_id,
        "connectionId": model.provider.id, "connectionLabel": model.provider.label,
        "enabled": bool(model.enabled),
        "ready": model.provider.credential_status is CredentialStatus.CONFIGURED,
    }


@router.get("/catalog")
def catalog() -> dict[str, Any]:
    """The roster as maker -> family -> version, for browsing.

    Built from the models this install actually has, never from a shipped
    list. Grouped by who MAKES the model, not by the provider it is reached
    through, so a model resold by a gateway appears beside the same maker's
    models reached directly. `unknown` is a real group and sorted last.
    """
    by_maker: dict[str, dict[str, Any]] = {}
    for model in list_models():
        maker = by_maker.setdefault(model.maker, {"id": model.maker, "label": model.maker,
                                                   "families": {}})
        family_id = model.family or model.native_model_id
        family = maker["families"].setdefault(
            family_id, {"id": family_id, "label": model.family or model.native_model_id,
                        "versions": {}})
        node = family["versions"].setdefault(
            model.native_model_id,
            {"model": model.native_model_id, "label": model.display_name or model.native_model_id,
             "version": _version_of(model), "deployments": []})
        node["deployments"].append(_catalog_route(model))

    def sorted_maker(row: dict[str, Any]) -> dict[str, Any]:
        families = [
            {**family, "versions": sorted(family["versions"].values(), key=lambda v: v["model"])}
            for family in sorted(row["families"].values(), key=lambda f: f["label"])
        ]
        return {**row, "families": families}

    ordered = sorted(by_maker.values(), key=lambda p: (p["id"] == "unknown", p["id"]))
    return {"providers": [sorted_maker(row) for row in ordered]}


@router.get("/providers")
def provider_tiles() -> dict[str, Any]:
    """The five user-facing tiles the "add a model" flow shows. See
    `_PROVIDER_TILES`'s own docstring for why this is a frozen shape of its
    own rather than derived from `model_system.providers.BUILTIN_TEMPLATES`.
    """
    return {"providers": [dict(t) for t in _PROVIDER_TILES]}


# --- adding, changing, removing --------------------------------------------

@connections_router.post("")
def add(body: dict[str, Any] = Body(default_factory=dict)):
    if not body.get("provider") and not body.get("adapter"):
        return JSONResponse({"ok": False, "error": "Please choose a provider."}, status_code=400)
    try:
        result = setup.create_provider_with_models(
            template=body.get("provider"), adapter=body.get("adapter"),
            base_url=body.get("baseUrl"), label=body.get("label"), secret=body.get("secret"),
            models=body.get("models"), resolved=body.get("resolved"))
    except (ValueError, KeyError) as err:
        return JSONResponse({"ok": False, "error": str(err) or "Could not add that connection."},
                            status_code=400)
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)

    models = list_models()
    return {"ok": True, "connection": _public_connection(result["provider"], models),
            "added": [_public_model(m) for m in result["added"]],
            "failed": result["failed"], "steps": result.get("steps")}


@connections_router.post("/probe")
def probe(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    """Try an address whose wire format is not known yet.

    Answers with every attempt it made, in plain language, so a failure can be
    explained rather than reduced to one generic sentence. A 401 with no key
    supplied is "reached it, needs a key", never "that key is invalid".
    """
    result = probe_endpoint(str(body.get("baseUrl") or ""), body.get("secret"))
    return {"ok": result.ok, "steps": result.steps, "adapter": result.adapter,
            "baseUrl": result.base_url, "kind": result.kind,
            "keyRequired": result.key_required, "models": result.models,
            "error": result.error, "needsKey": result.needs_key}


@connections_router.post("/discover")
def discover(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return setup.discover_models(
        template=body.get("provider"), adapter=body.get("adapter") or "openai_compatible",
        base_url=body.get("baseUrl"), secret=body.get("secret"),
        provider_id=body.get("connectionId"))


@connections_router.patch("/{connection_id}")
def edit_connection(connection_id: str, body: dict[str, Any] = Body(default_factory=dict)):
    patch = {}
    if "label" in body:
        patch["label"] = body["label"]
    if "baseUrl" in body:
        patch["base_url"] = body["baseUrl"]
    if "secret" in body:
        patch["secret"] = body["secret"]
    try:
        provider = update_provider(connection_id, patch)
    except KeyError:
        return JSONResponse({"ok": False, "error": "Unknown connection."}, status_code=404)
    except ValueError as err:
        return JSONResponse({"ok": False, "error": str(err)}, status_code=400)
    return {"ok": True, "connection": _public_connection(provider, list_models())}


@connections_router.delete("/{connection_id}")
def remove_connection(connection_id: str) -> dict[str, Any]:
    """Removing a connection removes the models that hung off it — they cannot
    answer without it. The count is reported so the screen can say so."""
    removed = remove_provider(connection_id)
    return {"ok": True, "removedModels": removed}


@router.post("")
def add_models(body: dict[str, Any] = Body(default_factory=dict)):
    """More models under a connection that already works.

    No test: the address and key were validated when the connection was
    added, and re-testing here would mean one live API call per model.
    """
    connection_id = body.get("connectionId")
    models = body.get("models")
    if not connection_id or not isinstance(models, list) or not models:
        return JSONResponse({"ok": False, "error": "Pick a connection and at least one model."},
                            status_code=400)
    try:
        result = setup.add_models_to_provider(connection_id, models)
    except KeyError:
        return JSONResponse({"ok": False, "error": "Unknown connection."}, status_code=404)
    return {"ok": True, "added": [_public_model(m) for m in result["added"]],
            "failed": result["failed"]}


def _model_patch(body: dict[str, Any]) -> dict[str, Any]:
    """Translate the wire's `{label, enabled, notes, overrides}` shape into
    `registry.update_model`'s patch keys.

    `overrides` is user-owned data reachable straight from a PATCH body, so it
    is never trusted to be shaped like anything here either — a malformed
    `overrides.capabilities` degrades to "no override" rather than raising,
    the same way `registry._row_to_resolved` degrades a hostile stored value
    at read time. Two guards for the same fact: this one keeps garbage from
    ever being written, `_mapping()` in the registry keeps garbage already
    written (or a hand-edited row) from taking a read down.
    """
    patch: dict[str, Any] = {}
    for key in ("label", "enabled", "notes"):
        if key in body:
            patch[key] = body[key]
    overrides = body.get("overrides")
    if isinstance(overrides, dict):
        if "capabilities" in overrides and isinstance(overrides["capabilities"], dict):
            patch["capability_overrides"] = overrides["capabilities"]
        if "effort" in overrides and isinstance(overrides["effort"], dict):
            patch["reasoning_override"] = overrides["effort"]
        if "quality" in overrides:
            quality = overrides["quality"]
            if isinstance(quality, (int, float)) and not isinstance(quality, bool) and 0 <= quality <= 5:
                patch["quality"] = int(quality)
    return patch


@router.patch("/{model_id}")
def edit_model(model_id: str, body: dict[str, Any] = Body(default_factory=dict)):
    try:
        entry = update_model(model_id, _model_patch(body))
    except KeyError:
        return JSONResponse({"ok": False, "error": "Unknown model."}, status_code=404)
    return {"ok": True, "model": _public_model(entry)}


@router.delete("/{model_id}")
def remove_model(model_id: str) -> dict[str, Any]:
    delete_model(model_id)
    return {"ok": True}


# --- is it actually working ------------------------------------------------

def _test_and_record(model: Any) -> dict[str, Any]:
    """Test one model and let the result update its availability badge.

    Bookkeeping must never turn a working test into a failed request, so a
    failure to RECORD is logged and swallowed — the test's own answer stands.
    """
    kind = ErrorKind.UNKNOWN
    try:
        result = get_adapter(model.provider.adapter).test_connection(model.provider,
                                                                      model.native_model_id)
    except Exception as err:  # noqa: BLE001 — an unreachable model is a result
        # The RAW error classifies more accurately than the sentence an
        # adapter already reduced it to: rate-limited and misconfigured are
        # different states with different retry behaviour.
        kind = classify(err)
        result = {"ok": False, "error": str(err) or "That model could not be reached."}

    try:
        if result.get("ok"):
            health.clear(model.id)
        else:
            health.record_failure(model.id, kind, detail=result.get("error"))
    except Exception:  # noqa: BLE001
        logger.exception("could not record health for %s", model.id)
    return {"ok": result.get("ok", False), "error": result.get("error")}


@router.post("/{model_id}/test")
def test_model(model_id: str):
    entry = get_model(model_id)
    if entry is None:
        return JSONResponse({"ok": False, "error": "Unknown model."}, status_code=404)
    return _test_and_record(entry)


@router.get("/recheck/preview")
def recheck_preview() -> dict[str, Any]:
    """What checking everything would cost, before anyone presses it.

    Read-only — it makes no model calls. It only reports what each provider
    has already told us about the credit left, where a provider reports that
    at all.
    """
    from ..cost import store as cost_store

    enabled = [m for m in list_models() if m.enabled]
    not_working = [m for m in enabled if not health.is_eligible(m.id)]

    by_connection = []
    for provider in list_providers():
        count = sum(1 for m in enabled if m.provider.id == provider.id)
        if not count:
            continue
        balance = cost_store.get_balance(provider.id) or {}
        by_connection.append({"id": provider.id, "label": provider.label, "count": count,
                              "isFreeTier": balance.get("isFreeTier"),
                              "remaining": balance.get("remaining")})
    return {"total": len(enabled), "notWorking": len(not_working), "byConnection": by_connection}


@router.post("/recheck")
def recheck(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    """`scope: "all"` checks every enabled model; anything else checks only the
    ones not currently working — the cheap default, since a model already
    answering needs no proof."""
    enabled = [m for m in list_models() if m.enabled]
    entries = (enabled if body.get("scope") == "all"
               else [m for m in enabled if not health.is_eligible(m.id)])

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

    return {"ok": True, "models": [_public_model(m) for m in list_models()]}
