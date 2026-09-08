"""Reading and writing API keys and the active provider, to a local .env file.

A faithful port of server/config.js. Kept dependency-free on purpose (no
python-dotenv) — it's a tiny format, no need to add a dependency just to parse
"KEY=value" lines, and the original made the same call for the same reason.

The write format must stay byte-compatible with the Node implementation for the
duration of the migration: `KEY=value` lines joined by "\\n" with a single
trailing newline, and falsy (empty) values dropped entirely.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

_DEFAULT_ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"

ENV_KEYS = {
    "gemini": "GEMINI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}
ACTIVE_PROVIDER_VAR = "JARVIS_ACTIVE_PROVIDER"
DEFAULT_PROVIDER = "gemini"
SECRET_PREFIX = "JARVIS_SECRET_"


def env_file_path() -> Path:
    """The resolved .env path — for a caller (the ops config-integrity check)
    that needs to watch the real file directly rather than going through this
    module's own read/write functions."""
    override = os.environ.get("JARVIS_ENV_PATH")
    return Path(override) if override else _DEFAULT_ENV_PATH


def _parse_env_file(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        eq = line.find("=")
        if eq == -1:
            continue
        key = line[:eq].strip()
        value = line[eq + 1 :].strip()
        # Strip surrounding quotes if present.
        if len(value) >= 2 and (
            (value.startswith('"') and value.endswith('"'))
            or (value.startswith("'") and value.endswith("'"))
        ):
            value = value[1:-1]
        result[key] = value
    return result


def _read_env_file() -> dict[str, str]:
    path = env_file_path()
    if not path.exists():
        return {}
    return _parse_env_file(path.read_text(encoding="utf-8"))


# Same "this app legitimately wrote this exact content just now" record store.py
# keeps for data/*.json, mirrored here for the one file that never goes through
# store.py at all. Records a content hash rather than just a timestamp — see
# store.py's own note on why.
_env_last_write: dict[str, object] | None = None


def env_last_write_time() -> dict[str, object] | None:
    """`{ts, hash}` of THIS process's last _write_env_file() — None if never."""
    return _env_last_write


def _write_env_file(values: dict[str, str]) -> None:
    global _env_last_write
    lines = [f"{k}={v}" for k, v in values.items() if v]
    contents = "\n".join(lines) + "\n"
    env_file_path().parent.mkdir(parents=True, exist_ok=True)
    env_file_path().write_text(contents, encoding="utf-8")
    _env_last_write = {
        "ts": int(time.time() * 1000),
        "hash": hashlib.sha256(contents.encode("utf-8")).hexdigest(),
    }


def get_provider_key(provider: str) -> str | None:
    """The saved API key for a provider ('gemini'|'anthropic'|'openai'), or None."""
    env_var = ENV_KEYS.get(provider)
    if not env_var:
        return None
    # Allow an environment variable to override the file, for advanced users.
    if os.environ.get(env_var):
        return os.environ[env_var]
    return _read_env_file().get(env_var) or None


def save_provider_key(provider: str, key: str) -> None:
    """Saves a provider's API key to the local .env file (creates it if needed)."""
    env_var = ENV_KEYS.get(provider)
    if not env_var:
        raise ValueError(f"Unknown provider: {provider}")
    trimmed = str(key or "").strip()
    values = _read_env_file()
    values[env_var] = trimmed
    _write_env_file(values)
    os.environ[env_var] = trimmed


def get_configured_providers() -> list[str]:
    """Which providers currently have a saved, non-empty key."""
    return [p for p in ENV_KEYS if get_provider_key(p)]


def get_active_provider() -> str:
    """Which provider is currently selected to drive the conversation."""
    return _read_env_file().get(ACTIVE_PROVIDER_VAR) or DEFAULT_PROVIDER


def set_active_provider(provider: str) -> None:
    if provider not in ENV_KEYS:
        raise ValueError(f"Unknown provider: {provider}")
    values = _read_env_file()
    values[ACTIVE_PROVIDER_VAR] = provider
    _write_env_file(values)


# --- Backward-compatible Gemini-specific helpers ---
# Gemini is also used directly for turn-check regardless of which provider is
# driving the conversation, so these convenience wrappers stay.

def get_api_key() -> str | None:
    return get_provider_key("gemini")


def save_api_key(key: str) -> None:
    save_provider_key("gemini", key)


# --- Generic secrets, for the model registry ---
# Any model entry can point at a `secretRef` the registry makes up when the
# model is added — stored as JARVIS_SECRET_<REF>. Exception: the three original
# built-in providers keep using their original ENV_KEYS entry, so migrating an
# existing .env doesn't duplicate a key into a second variable —
# get_secret('gemini') and get_provider_key('gemini') read the same value.

def _secret_env_var(ref: str) -> str:
    return f"{SECRET_PREFIX}{str(ref or '').upper()}"


def get_secret(ref: str) -> str | None:
    """The saved secret for a given ref, or None."""
    if not ref:
        return None
    if ref in ENV_KEYS:
        return get_provider_key(ref)
    env_var = _secret_env_var(ref)
    if os.environ.get(env_var):
        return os.environ[env_var]
    return _read_env_file().get(env_var) or None


def save_secret(ref: str, value: str) -> None:
    """Saves a secret under a ref (creates the .env entry if needed)."""
    if not ref:
        raise ValueError("save_secret needs a ref.")
    if ref in ENV_KEYS:
        save_provider_key(ref, value)
        return
    env_var = _secret_env_var(ref)
    trimmed = str(value or "").strip()
    values = _read_env_file()
    values[env_var] = trimmed
    _write_env_file(values)
    os.environ[env_var] = trimmed


def secret_values() -> list[str]:
    """Every secret value this install currently holds, for redaction.

    Values, not names: `redact.py` needs to find them inside text a provider
    sent back. Read from BOTH the file and the environment, because the two can
    disagree — a key set as a real environment variable by an advanced user is
    never in the file, and a key saved before this process started is in the
    file whether or not anything has read it yet.

    Lives here rather than in redact.py so that what counts as a secret is
    defined once, by the module that writes them.
    """
    found: list[str] = []
    from_file = _read_env_file()
    for name, value in list(from_file.items()) + list(os.environ.items()):
        if name in ENV_KEYS.values() or name.startswith(SECRET_PREFIX):
            if value and value not in found:
                found.append(value)
    return found


def delete_secret(ref: str) -> None:
    """Removes a saved secret. Empty values are dropped by _write_env_file."""
    if not ref:
        return
    env_var = ENV_KEYS.get(ref) or _secret_env_var(ref)
    values = _read_env_file()
    values.pop(env_var, None)
    _write_env_file(values)
    os.environ.pop(env_var, None)
