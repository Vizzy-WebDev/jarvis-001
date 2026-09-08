"""Adding a connection, and finding out what it can do.

The orchestration between the stores (`connections.py`, `registry.py`) and the
adapters: discovery, the reachability check, and the one call that creates a
connection with its first models. Kept out of the route module so the logic is
testable without HTTP, and out of the stores so they stay leaves.

**The reachability rule is the part worth reading.** Adding several models is
NOT validated by generating with one of them. A gateway's model LISTING can
succeed while one specific routed model's own upstream key or quota fails, and
the original rejected a real, working connection over exactly one bad route
among 115 good ones. So: one model — test that model, which is the more specific
question. Several — prove the address and key by listing, and let a single bad
route fail later, where it can be seen.
"""

from __future__ import annotations

import logging
from typing import Any

from ..adapters import get_adapter
from ..redact import redact
from .catalog import infer_billing
from .connections import add_connection, get_connection
from .error_kind import find_message
from .probe import probe_endpoint
from .providers import get_provider, provider_for_legacy
from .registry import add_models, list_models

logger = logging.getLogger(__name__)


def _plain(module: Any, err: BaseException, fallback: str) -> str:
    """The sentence the user reads. Every SDK buries it somewhere different, so
    the adapter's own extractor is asked first and `find_message` is the
    backstop — never the raw `str(err)`, which is JSON on all three."""
    for attempt in (lambda: module.friendly_error(err), lambda: find_message(err)):
        try:
            text = attempt()
        except Exception:  # noqa: BLE001 — an error helper must not mask the error
            continue
        if isinstance(text, str) and text.strip():
            return redact(text)
    return fallback


def _entry_for(adapter: str | None, base_url: str | None, secret: str | None,
               secret_ref: str | None = None, model: str | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"adapter": adapter, "baseUrl": base_url}
    if secret_ref:
        entry["secretRef"] = secret_ref
    # Present-but-None is meaningful to an adapter: it means "no key was given",
    # which is different from "look one up".
    if secret is not None:
        entry["secretValue"] = secret
    if model:
        entry["model"] = model
    return entry


def _normalise(models: Any, adapter: str | None, base_url: str | None,
               kind: str | None) -> list[dict[str, Any]]:
    out = []
    for raw in models or []:
        item = {"model": raw} if isinstance(raw, str) else dict(raw)
        if not item.get("model"):
            continue
        item.setdefault("label", item["model"])
        item.setdefault("contextTokens", None)
        if item.get("billing") is None:
            item["billing"] = infer_billing(adapter, base_url, item["model"], kind)
        out.append(item)
    return out


def discover_models(*, adapter: str | None = None, base_url: str | None = None,
                    secret: str | None = None,
                    connection_id: str | None = None) -> dict[str, Any]:
    """What models are available at an address. Always answers.

    `{models, error}` rather than an empty list either way: "reached it, found
    nothing" and "could not reach it at all" are different problems with
    different fixes, and a caller that cannot tell them apart shows the wrong
    advice.

    `connection_id` reuses a saved connection's key instead of asking for it
    again — and filters out models already added under THAT connection, since
    (connection, model) is the uniqueness rule. The same model under a different
    connection is untouched and still offered.
    """
    resolved_adapter, resolved_base, secret_ref, kind = adapter, base_url, None, None
    if connection_id:
        conn = get_connection(connection_id)
        if conn is not None:
            resolved_adapter = conn.get("adapter")
            resolved_base = conn.get("baseUrl")
            secret_ref = conn.get("secretRef")
            kind = conn.get("kind") or provider_for_legacy(conn.get("adapter"),
                                                           conn.get("baseUrl"))["kind"]

    module = get_adapter(resolved_adapter or "openai-compatible")
    try:
        found = module.list_models(_entry_for(resolved_adapter, resolved_base, secret, secret_ref))
    except Exception as err:  # noqa: BLE001 — an unreachable address is an answer
        logger.info("discovery failed for %s: %s", resolved_adapter, redact(str(err)))
        return {"models": [],
                "error": _plain(module, err, "Could not discover models at that address.")}

    items = _normalise(found, resolved_adapter, resolved_base, kind)
    if connection_id:
        already = {e.get("model") for e in list_models() if e.get("connectionId") == connection_id}
        items = [item for item in items if item["model"] not in already]
    return {"models": items, "error": None}


def verify_reachability(*, adapter: str | None, base_url: str | None,
                        secret: str | None) -> dict[str, Any]:
    """Prove the address and key by listing — the check for a multi-model add.

    Deliberately weaker than generating: see this module's own note. It answers
    "is this connection real", not "does every model behind it work", which is
    the honest question when several were picked at once.
    """
    module = get_adapter(adapter or "openai-compatible")
    try:
        module.list_models(_entry_for(adapter, base_url, secret))
    except Exception as err:  # noqa: BLE001
        return {"ok": False, "error": _plain(module, err, "Could not reach that address."),
                "detail": redact(str(err), extra=(secret,) if secret else ())}
    return {"ok": True}


def test_model(*, adapter: str | None, model: str, base_url: str | None,
               secret: str | None) -> dict[str, Any]:
    """Does THIS model actually produce a token — the check for a single add."""
    module = get_adapter(adapter or "openai-compatible")
    result = module.test_connection(_entry_for(adapter, base_url, secret, model=model))
    if not result.get("ok") and result.get("error"):
        result = {**result, "error": redact(str(result["error"]),
                                            extra=(secret,) if secret else ())}
    return result


def create_connection_with_models(*, provider: str | None = None, adapter: str | None = None,
                                  base_url: str | None = None, label: str | None = None,
                                  secret: str | None = None, models: list[Any] | None = None,
                                  resolved: dict[str, Any] | None = None) -> dict[str, Any]:
    """One saved address+key, plus the models picked under it."""
    wanted = [m for m in (models or []) if m]
    if not wanted:
        raise ValueError("Pick at least one model to add.")

    resolved_adapter, resolved_base = adapter, base_url
    kind: str | None = None
    key_required: bool | None = None
    steps: list[str] | None = None
    # True once connectivity is proven for THIS request — by the caller's own
    # earlier probe, or by the probe below, which validates by actually calling
    # the address rather than by guessing from its shape.
    proven = False

    if provider:
        row = get_provider(provider)
        if row is None:
            raise ValueError(f"Unknown provider: {provider}")
        if provider == "custom":
            if (resolved and resolved.get("adapter") and resolved.get("baseUrl")
                    and isinstance(resolved.get("keyRequired"), bool)):
                # The PROBE's base url, not the raw one: normalising it (trying
                # `/v1`, and so on) is part of what the probe resolved, and
                # using the typed address here would silently undo that.
                resolved_adapter = resolved["adapter"]
                resolved_base = resolved["baseUrl"]
                kind = resolved.get("kind")
                key_required = resolved["keyRequired"]
                proven = True
            else:
                probe = probe_endpoint(base_url or "", secret)
                steps = probe.steps
                if not probe.ok:
                    return {"ok": False, "error": probe.error,
                            "detail": redact(" ".join(probe.steps or []),
                                             extra=(secret,) if secret else ()),
                            "steps": steps}
                resolved_adapter, resolved_base = probe.adapter, probe.base_url
                kind, key_required = probe.kind, probe.key_required
                proven = True
        else:
            resolved_adapter = row["adapter"]
            resolved_base = base_url or row["baseUrl"] if row.get("urlEditable") else row["baseUrl"]
            kind, key_required = row["kind"], row["keyRequired"]

    if not proven:
        first = wanted[0]
        check = (verify_reachability(adapter=resolved_adapter, base_url=resolved_base, secret=secret)
                 if len(wanted) > 1
                 else test_model(adapter=resolved_adapter,
                                 model=first if isinstance(first, str) else first["model"],
                                 base_url=resolved_base, secret=secret))
        if not check.get("ok"):
            return {"ok": False, "error": check.get("error"), "detail": check.get("detail"),
                    "steps": steps}

    conn = add_connection(adapter=resolved_adapter, base_url=resolved_base, label=label,
                          secret=secret, provider=provider, kind=kind,
                          key_required=key_required)
    return {"ok": True, "connection": conn, **add_models(conn["id"], wanted), "steps": steps}
