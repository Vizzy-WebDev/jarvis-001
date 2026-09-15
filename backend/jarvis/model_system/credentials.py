"""Provider credentials — a thin, careful wrapper, never a second secret store.

Jarvis already has one place secrets are written to disk: `jarvis/config.py`'s
`.env` file, with `save_secret`/`get_secret`/`delete_secret` keyed by an
arbitrary ref. This module integrates with that rather than inventing another
mechanism — the project's own standing rule (§26): "if the application already
has a secure secret store, integrate with it."

**What this module refuses to do is the point of it.** A credential value
never appears in a return value here, is never logged, and is never handed to
a model. Every function below returns a `CredentialStatus`, never a secret —
the one exception is `resolve()`, which exists ONLY for an adapter's own
internal use building an outbound request, and is not re-exported through any
route.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from enum import Enum

from ..config import delete_secret, get_secret, save_secret


class CredentialStatus(Enum):
    """What a route or a screen may know about a credential.

    `CONNECTED` and `AUTH_FAILED` are not stored here — they are the outcome
    of an actual live check (`model_system/gateway.py`'s test-connection path) and are
    reported back to the caller of that check directly. This module alone can
    only ever tell "is something saved" — it makes no network call and holds
    no opinion about whether that value still works.
    """

    NOT_CONFIGURED = "not_configured"
    CONFIGURED = "configured"
    CONNECTED = "connected"
    AUTH_FAILED = "auth_failed"


@dataclass(frozen=True)
class Credential:
    ref: str
    status: CredentialStatus


def set_credential(ref: str, value: str) -> None:
    if not ref:
        raise ValueError("A credential needs a ref.")
    save_secret(ref, value)


def clear_credential(ref: str) -> None:
    if ref:
        delete_secret(ref)


def status_of(ref: str | None, *, key_required: bool | None = True) -> CredentialStatus:
    """`CONFIGURED`/`NOT_CONFIGURED` from what is saved — never a live check.

    `key_required=False` (a local server that takes no key) always reads as
    configured: there is nothing to configure. `None` (not yet established)
    is treated as requiring one, the safer default — a provider row that has
    never been probed should not claim to be ready.
    """
    if key_required is False:
        return CredentialStatus.CONFIGURED
    if ref and get_secret(ref):
        return CredentialStatus.CONFIGURED
    return CredentialStatus.NOT_CONFIGURED


def resolve(ref: str | None) -> str | None:
    """The real secret value, for an adapter building an outbound request
    ONLY. Never call this from a route handler or anything that returns to
    the frontend — that is precisely the boundary this module exists to hold.
    """
    if ref and ref in _transient:
        return _transient[ref]
    return get_secret(ref) if ref else None


# --- a value not yet worth saving ---------------------------------------------
#
# Probing an address (§14: "if discovery is unavailable, allow manual
# registration"; testing a connection before it exists) needs an adapter to
# send a real credential without writing it to disk first — a probe the user
# cancels must leave no trace in `.env`. In-memory, per-process, never
# persisted; `discard_transient` is called once the probe is done, whichever
# way it went.

_lock = threading.Lock()
_transient: dict[str, str] = {}


def stage_transient(value: str) -> str:
    """Hold a credential in memory only, and hand back a ref an adapter can
    resolve exactly like a saved one. Not written to disk until/unless a
    caller separately saves it under a real ref."""
    with _lock:
        ref = f"transient_{uuid.uuid4().hex}"
        _transient[ref] = value
        return ref


def discard_transient(ref: str | None) -> None:
    if not ref:
        return
    with _lock:
        _transient.pop(ref, None)
