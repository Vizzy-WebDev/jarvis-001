"""Adding a provider, and finding out what it can do.

The orchestration between the stores (`providers.py`, `registry.py`) and the
adapters: discovery, the reachability check, and the one call that creates a
provider with its first models. Kept out of the route module so the logic is
testable without HTTP, and out of the stores so they stay leaves.

**The reachability rule is the part worth reading.** Adding several models is
NOT validated by generating with one of them. A provider's model LISTING can
succeed while one specific routed model's own upstream key or quota fails, and
rejecting the whole address over one bad route among many working ones would
reject a real, working connection. So: one model — test that model, the more
specific question. Several — prove the address and key by listing, and let a
single bad route fail later, where it can be seen.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..redact import redact
from .adapters import get_adapter
from .credentials import discard_transient, stage_transient
from .discovery import for_picker
from .errors import find_message
from .probe import probe_endpoint
from .providers import (
    AuthMethod, Provider, ProviderKind, add_provider, get_provider, get_template,
)
from .registry import add_model, list_models

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


@dataclass(frozen=True)
class _DiscoveryProvider:
    """A `Provider`-shaped stand-in for an address being discovered or tested
    before (or without ever) being saved — everything an adapter's
    `discover_models`/`test_connection` reads, and nothing more. Mirrors
    `probe._ProbeProvider`."""

    base_url: str | None
    credential_ref: str | None
    key_required: bool | None


def _kind_of(value: str | None) -> ProviderKind | None:
    if value is None:
        return None
    try:
        return ProviderKind(value)
    except ValueError:
        return None


def discover_models(*, template: str | None = None, adapter: str | None = None,
                    base_url: str | None = None, secret: str | None = None,
                    provider_id: str | None = None) -> dict[str, Any]:
    """What models are available at an address. Always answers.

    `{models, error}` rather than an empty list either way: "reached it, found
    nothing" and "could not reach it at all" are different problems with
    different fixes.

    `template` resolves to a built-in preset's adapter/address the same way
    `create_provider_with_models()` resolves it for Save — so discovery for a
    first-party template (Gemini, Anthropic, ...) talks to THAT service's real
    API rather than silently falling back to the openai-compatible default
    just because no address was typed (their presets have none; the SDK needs
    none). "custom" has no preset to resolve, and an unrecognised template is
    not an error — `adapter`/`base_url` stand as given, same as Save.

    `provider_id` reuses an already-saved provider's credential instead of
    asking for it again, and filters out models already added under THAT
    provider — resolved after `template` so refreshing an already-saved
    provider always wins on its own facts.
    """
    resolved_adapter, resolved_base, ref, key_required = adapter, base_url, None, bool(secret)

    if template and template != "custom":
        preset = get_template(template)
        if preset is not None:
            resolved_adapter = preset["adapter"]
            resolved_base = (base_url or preset["base_url"]) if preset["url_editable"] else preset["base_url"]
            key_required = preset["key_required"]

    if provider_id:
        row = get_provider(provider_id)
        if row is not None:
            resolved_adapter = row.adapter
            resolved_base = row.base_url
            ref = row.credential_ref
            key_required = row.key_required

    module = get_adapter(resolved_adapter or "openai_compatible")
    staged = stage_transient(secret) if (secret and not ref) else None
    try:
        provider = _DiscoveryProvider(base_url=resolved_base, credential_ref=ref or staged,
                                      key_required=key_required)
        found = module.discover_models(provider)
    except Exception as err:  # noqa: BLE001 — an unreachable address is an answer
        logger.info("discovery failed for %s: %s", resolved_adapter, redact(str(err)))
        return {"models": [],
                "error": _plain(module, err, "Could not discover models at that address.")}
    finally:
        discard_transient(staged)

    items = for_picker(found)
    if provider_id:
        already = {m.native_model_id for m in list_models(provider_id)}
        items = [item for item in items if item["model"] not in already]
    return {"models": items, "error": None}


def verify_reachability(*, adapter: str | None, base_url: str | None,
                        secret: str | None) -> dict[str, Any]:
    """Prove the address and key by listing — the check for a multi-model add.

    Deliberately weaker than generating: see this module's own note. It answers
    "is this connection real", not "does every model behind it work", which is
    the honest question when several were picked at once.
    """
    module = get_adapter(adapter or "openai_compatible")
    staged = stage_transient(secret) if secret else None
    try:
        provider = _DiscoveryProvider(base_url=base_url, credential_ref=staged, key_required=bool(secret))
        module.discover_models(provider)
    except Exception as err:  # noqa: BLE001
        return {"ok": False, "error": _plain(module, err, "Could not reach that address."),
                "detail": redact(str(err), extra=(secret,) if secret else ())}
    finally:
        discard_transient(staged)
    return {"ok": True}


def test_model(*, adapter: str | None, model: str, base_url: str | None,
               secret: str | None) -> dict[str, Any]:
    """Does THIS model actually produce a token — the check for a single add."""
    module = get_adapter(adapter or "openai_compatible")
    staged = stage_transient(secret) if secret else None
    try:
        provider = _DiscoveryProvider(base_url=base_url, credential_ref=staged, key_required=bool(secret))
        result = module.test_connection(provider, model)
    finally:
        discard_transient(staged)
    if not result.get("ok") and result.get("error"):
        result = {**result, "error": redact(str(result["error"]),
                                            extra=(secret,) if secret else ())}
    return result


def create_provider_with_models(*, template: str | None = None, adapter: str | None = None,
                                base_url: str | None = None, label: str | None = None,
                                secret: str | None = None, models: list[Any] | None = None,
                                resolved: dict[str, Any] | None = None) -> dict[str, Any]:
    """One saved provider (address + key), plus the models picked under it."""
    wanted = [m for m in (models or []) if m]
    if not wanted:
        raise ValueError("Pick at least one model to add.")

    resolved_adapter, resolved_base = adapter, base_url
    kind: ProviderKind | None = None
    auth_method = AuthMethod.API_KEY
    key_required: bool | None = None
    builtin = False
    steps: list[str] | None = None
    # True once connectivity is proven for THIS request — by the caller's own
    # earlier probe, or by the probe below, which validates by actually calling
    # the address rather than by guessing from its shape.
    proven = False

    if template:
        preset = get_template(template)
        if preset is None:
            # An unrecognised template is not an error when the caller has said
            # how to talk to it. `BUILTIN_TEMPLATES` are setup PRESETS — a
            # shortcut that fills in an address and a wire format for the
            # common cases — never the complete set of providers that may
            # exist. A name nobody shipped is just a name nobody shipped.
            if not adapter:
                raise ValueError(
                    f"I don't know a provider called {template!r}, and no address was "
                    "given either — so there's nothing to connect to.")
        elif template == "custom":
            if (resolved and resolved.get("adapter") and resolved.get("baseUrl")
                    and isinstance(resolved.get("keyRequired"), bool)):
                # The PROBE's base url, not the raw one: normalising it (trying
                # `/v1`, and so on) is part of what the probe resolved, and
                # using the typed address here would silently undo that.
                resolved_adapter = resolved["adapter"]
                resolved_base = resolved["baseUrl"]
                kind = _kind_of(resolved.get("kind"))
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
                kind, key_required = _kind_of(probe.kind), probe.key_required
                proven = True
            auth_method = AuthMethod.NONE if key_required is False else AuthMethod.API_KEY
        else:
            resolved_adapter = preset["adapter"]
            resolved_base = (base_url or preset["base_url"]) if preset["url_editable"] else preset["base_url"]
            kind, key_required = preset["kind"], preset["key_required"]
            auth_method = preset["auth_method"]
            builtin = True

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

    # A recognised template gets its nice display name; an unrecognised one
    # (a provider "nobody shipped") keeps the exact name the caller gave it,
    # rather than being silently relabelled after its wire format — the same
    # flexibility `BUILTIN_TEMPLATES` being presets, not a closed enum, is
    # meant to offer one layer up.
    preset = get_template(template) if template else None
    preset_label = preset["label"] if preset else template
    provider = add_provider(
        label=label or preset_label or (resolved_adapter or "Provider").replace("_", " ").title(),
        kind=kind or ProviderKind.OPENAI_COMPATIBLE,
        adapter=resolved_adapter or "openai_compatible", base_url=resolved_base,
        auth_method=auth_method, secret=secret, key_required=key_required, builtin=builtin,
    )
    added, failed = _add_models(provider, wanted)
    return {"ok": True, "provider": provider, "added": added, "failed": failed, "steps": steps}


def add_models_to_provider(provider_id: str, models: list[Any]) -> dict[str, Any]:
    """More models under a provider that already works.

    No test: the address and key were validated when the provider was added,
    and re-testing here would mean one live API call per model.
    """
    provider = get_provider(provider_id)
    if provider is None:
        raise KeyError(f"Unknown provider: {provider_id}")
    wanted = [m for m in (models or []) if m]
    added, failed = _add_models(provider, wanted)
    return {"added": added, "failed": failed}


def _add_models(provider: Provider, wanted: list[Any]) -> tuple[list[Any], list[dict[str, Any]]]:
    """Several at once, reporting each failure rather than stopping. One bad
    name among fifteen should not lose the other fourteen."""
    added: list[Any] = []
    failed: list[dict[str, Any]] = []
    for item in wanted:
        native_id = item if isinstance(item, str) else str(item.get("model") or "")
        if not native_id:
            failed.append({"model": item, "error": "No model id given."})
            continue
        discovered = {} if isinstance(item, str) else {k: v for k, v in item.items() if k != "model"}
        try:
            added.append(add_model(provider_id=provider.id, native_model_id=native_id,
                                   label=discovered.get("label"), discovered=discovered))
        except ValueError as err:
            failed.append({"model": native_id, "error": str(err)})
    return added, failed
