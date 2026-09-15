"""Working out how to talk to an address the user just typed (§14, §23).

A Custom provider is PROBED, not guessed in the dark: every attempt is
narrated into `steps`, shown on success and on failure alike. The
alternative — a single "That connection didn't work." — is what leaves a
real, working address unusable with no way to find out why.

**Listing and generating are different permissions, and this checks both.**
A connection that only proves it can list models can still 401 on every
real turn if the key it was saved with turns out not to cover generation —
so the cascade below uses cheap discovery-shaped requests to work out the
WIRE FORMAT, then spends exactly one minimal generation call to confirm the
credential actually generates. Once, at add time — never per model, never
during routing.

A 401 with no key supplied is reported as "reached it, needs a key," never
as "that key is invalid" — there was no key to be invalid.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from .adapters import anthropic, gemini, openai_compatible
from .credentials import discard_transient, stage_transient
from .errors import classify

_LOCAL_HOST = re.compile(r"^(localhost|127\.0\.0\.1|::1|0\.0\.0\.0)$", re.I)
#: RFC 1918 plus .local — a self-hosted gateway on a home LAN is 'local',
#: not 'aggregator'.
_PRIVATE_HOST = re.compile(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)|\.local$", re.I)


@dataclass(frozen=True)
class _ProbeProvider:
    """A `Provider`-shaped stand-in for an address that has not been saved
    yet — everything an adapter's `test_connection`/`discover_models` reads,
    and nothing more."""

    base_url: str | None
    credential_ref: str | None
    key_required: bool | None


@dataclass
class ProbeResult:
    ok: bool
    steps: list[str] = field(default_factory=list)
    adapter: str | None = None
    base_url: str | None = None
    kind: str | None = None
    key_required: bool | None = None
    models: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    #: True when the address answered but a credential is missing.
    needs_key: bool = False


def normalize_base_url(raw: str) -> str:
    """Strip a trailing slash and a pasted `/chat/completions` — people copy
    an example curl command far more often than they read the base-URL field."""
    url = str(raw or "").strip().rstrip("/")
    return re.sub(r"/chat/completions$", "", url, flags=re.I)


def _candidates(base: str) -> list[str]:
    if re.search(r"/v1$|/api/v1$", base, re.I):
        return [base]
    return [base, f"{base}/v1"]


def _host_of(url: str) -> str:
    try:
        return urlparse(url).hostname or ""
    except ValueError:
        return ""


def _kind_for(base_url: str) -> str:
    from .providers import ProviderKind

    host = _host_of(base_url)
    if _LOCAL_HOST.match(host) or _PRIVATE_HOST.search(host):
        return ProviderKind.LOCAL.value
    return ProviderKind.AGGREGATOR.value


def probe_endpoint(base_url: str, secret: str | None = None) -> ProbeResult:
    """Resolve adapter / base_url / kind / key_required for an unsaved
    address. Always returns; never raises. Tries the OpenAI-compatible shape
    first (it covers the overwhelming majority of gateways and self-hosted
    servers), then Anthropic's, then Gemini's.
    """
    result = ProbeResult(ok=False)
    base = normalize_base_url(base_url)
    if not base:
        result.error = "Please enter a server address."
        return result

    ref = stage_transient(secret) if secret else None
    try:
        return _probe(base, secret, ref, result)
    finally:
        discard_transient(ref)


def _probe(base: str, secret: str | None, ref: str | None, result: ProbeResult) -> ProbeResult:
    saw_auth_wall = False

    for module, label in ((openai_compatible, "OpenAI-style"),
                          (anthropic, "Anthropic-style"),
                          (gemini, "Gemini-style")):
        for candidate in _candidates(base):
            result.steps.append(f"Trying {candidate} as a {label} server…")
            provider = _ProbeProvider(base_url=candidate, credential_ref=ref,
                                      key_required=bool(secret))
            try:
                models = module.discover_models(provider)
            except Exception as err:  # noqa: BLE001
                kind = classify(err).value
                if kind in ("authentication", "permission_denied"):
                    saw_auth_wall = True
                    result.steps.append(
                        f"  reached it, but it wants a key ({kind})."
                        if not secret else "  reached it, but rejected that key.")
                    # Reached, and the shape is right — a credential problem,
                    # not a shape problem. Stop cascading here.
                    result.adapter = module.NAME
                    result.base_url = candidate
                    result.kind = _kind_for(candidate)
                    result.key_required = True
                    result.needs_key = not secret
                    result.error = ("That server was reached but needs an API key."
                                    if not secret else "That server rejected that key.")
                    return result
                result.steps.append(f"  no: {_short(err)}")
                continue

            result.steps.append(f"  it answered, and listed {len(models)} model(s).")
            confirmed = _confirm_generation(module, candidate, ref, secret, models, result)
            if confirmed is not None:
                return confirmed

    result.error = ("That server needs an API key." if saw_auth_wall
                    else "I couldn't work out how to talk to that address.")
    result.needs_key = saw_auth_wall
    return result


def _confirm_generation(module: Any, base_url: str, ref: str | None, secret: str | None,
                        models: list[dict[str, Any]], result: ProbeResult) -> ProbeResult | None:
    """One real generation call — the step a listing-only check never takes.

    Without it, "the address answered" is mistaken for "this connection
    works," and a keyless save 401s on the first real question instead of
    here, where it can still be fixed.
    """
    if not models:
        result.steps.append("  but it listed no models, so there's nothing to try.")
        return None

    first = models[0].get("model")
    result.steps.append(f"  asking {first} for one word, to prove it can actually answer…")
    provider = _ProbeProvider(base_url=base_url, credential_ref=ref, key_required=bool(secret))
    outcome = module.test_connection(provider, first)
    if not outcome.get("ok"):
        message = outcome.get("error") or "it couldn't answer."
        result.steps.append(f"  no: {message}")
        result.adapter = module.NAME
        result.base_url = base_url
        result.kind = _kind_for(base_url)
        result.key_required = True if "key" in message.lower() else None
        result.needs_key = "key" in message.lower() and not secret
        result.error = message
        return result

    result.ok = True
    result.adapter = module.NAME
    result.base_url = base_url
    result.kind = _kind_for(base_url)
    # Only now is this a FACT: it generated with exactly the credential supplied.
    result.key_required = bool(secret)
    # The same shaping discovery applies, from the one function that does it —
    # a custom address and a known provider must group a model the same way.
    from .discovery import for_picker

    result.models = for_picker(models)
    result.steps.append("  it answered. This connection works.")
    return result


def _short(err: BaseException) -> str:
    text = str(err).strip().splitlines()[0] if str(err).strip() else err.__class__.__name__
    return text[:160]
