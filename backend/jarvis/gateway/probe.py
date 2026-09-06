"""Working out how to talk to an address the user just typed.

A Custom connection is PROBED, not guessed in the dark, and the probe shows its
working: every attempt is narrated into `steps`, which the UI shows on success
and on failure alike. The alternative — a single "That connection didn't work."
— is what left a real gateway connection unusable with no way to find out why.

**The fix this build makes over the Node original (§26).** That probe concluded
from a successful `/models` listing that the address worked and, if no key had
been supplied, that none was needed. Listing and generating are different
permissions: the connection saved as keyless, then 401'd on every real turn and
benched itself for six hours. So the cascade below still uses cheap
discovery-shaped requests to work out the SHAPE, and then spends exactly ONE
minimal generation call to confirm the credential actually generates. One call,
once, at add time — not per model, and never during routing.

A 401 with no key supplied is reported as "reached it, needs a key", never as
"that key is invalid": there was no key to be invalid.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from ..adapters import anthropic_adapter, gemini_adapter, openai_compatible
from .catalog import infer_billing
from .error_kind import classify_error

_LOCAL_HOST = re.compile(r"^(localhost|127\.0\.0\.1|::1|0\.0\.0\.0)$", re.I)
#: RFC 1918 plus .local — a self-hosted gateway on a home LAN is 'local',
#: not 'gateway'.
_PRIVATE_HOST = re.compile(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)|\.local$", re.I)


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
    #: True when the address answered but a credential is missing — a different
    #: thing from a wrong credential, and worded differently to the user.
    needs_key: bool = False


def normalize_base_url(raw: str) -> str:
    """Strip a trailing slash and a pasted `/chat/completions` — people copy an
    example curl command far more often than they read the base-URL field."""
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
    host = _host_of(base_url)
    if _LOCAL_HOST.match(host) or _PRIVATE_HOST.search(host):
        return "local"
    return "gateway"


def probe_endpoint(base_url: str, secret: str | None = None) -> ProbeResult:
    """Resolve adapter / baseUrl / kind / keyRequired for an unsaved address.

    Always returns; never raises. Tries the OpenAI chat-completions shape first
    (it covers the overwhelming majority of gateways and self-hosted servers),
    then Anthropic's, then Gemini's.
    """
    result = ProbeResult(ok=False)
    base = normalize_base_url(base_url)
    if not base:
        result.error = "Please enter a server address."
        return result

    saw_auth_wall = False

    for adapter, label in ((openai_compatible, "OpenAI-style"),
                           (anthropic_adapter, "Anthropic-style"),
                           (gemini_adapter, "Gemini-style")):
        for candidate in _candidates(base):
            result.steps.append(f"Trying {candidate} as a {label} server…")
            entry = {"model": "", "baseUrl": candidate, "secretValue": secret,
                     "keyRequired": bool(secret)}
            try:
                models = adapter.list_models(entry)
            except Exception as err:  # noqa: BLE001
                kind = classify_error(err)
                if kind in ("auth", "no_access"):
                    saw_auth_wall = True
                    result.steps.append(
                        f"  reached it, but it wants a key ({kind})."
                        if not secret else "  reached it, but rejected that key.")
                    # Reached, and the shape is right — a credential problem, not
                    # a shape problem. Stop cascading; trying other shapes here
                    # would report the wrong failure.
                    result.adapter = adapter.name
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
            confirmed = _confirm_generation(adapter, candidate, secret, models, result)
            if confirmed is not None:
                return confirmed

    result.error = ("That server needs an API key." if saw_auth_wall
                    else "I couldn't work out how to talk to that address.")
    result.needs_key = saw_auth_wall
    return result


def _confirm_generation(adapter, base_url: str, secret: str | None,
                        models: list[dict[str, Any]], result: ProbeResult) -> ProbeResult | None:
    """One real generation call — the step the Node probe never took.

    Without it, "the address answered" is mistaken for "this connection works",
    and a keyless save 401s on the user's first real question instead of here,
    where it can still be fixed.
    """
    if not models:
        result.steps.append("  but it listed no models, so there's nothing to try.")
        return None

    first = models[0].get("model")
    result.steps.append(f"  asking {first} for one word, to prove it can actually answer…")
    entry = {"model": first, "baseUrl": base_url, "secretValue": secret,
             "keyRequired": bool(secret)}
    outcome = adapter.test_connection(entry)
    if not outcome.get("ok"):
        message = outcome.get("error") or "it couldn't answer."
        result.steps.append(f"  no: {message}")
        result.adapter = adapter.name
        result.base_url = base_url
        result.kind = _kind_for(base_url)
        # Listing worked and generating did not. Whatever else is true, this
        # connection is not usable as saved — say so rather than storing it.
        result.key_required = True if "key" in message.lower() else None
        result.needs_key = "key" in message.lower() and not secret
        result.error = message
        return result

    kind = _kind_for(base_url)
    result.ok = True
    result.adapter = adapter.name
    result.base_url = base_url
    result.kind = kind
    # Only now is this a FACT: it generated with exactly the credential supplied.
    result.key_required = bool(secret)
    result.models = [
        {**m, "billing": m.get("billing") or infer_billing(adapter.name, base_url, m.get("model"), kind)}
        for m in models
    ]
    result.steps.append("  it answered. This connection works.")
    return result


def _short(err: BaseException) -> str:
    text = str(err).strip().splitlines()[0] if str(err).strip() else err.__class__.__name__
    return text[:160]
