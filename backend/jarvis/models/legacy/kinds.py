"""The kinds of provider a person can connect, and which wire format each speaks.

Six kinds, four formats. A kind is what someone picks in the interface; a format
is what the code has to speak on the wire. The two differ on purpose: Ollama and
LM Studio are different programs that both genuinely answer OpenAI's chat
format, so they are two kinds sharing one implementation — and `custom` exists
so a provider that speaks Anthropic's or Gemini's format is not forced through
OpenAI's.

Every kind listed here has a real implementation behind it in `providers/`. A
kind that could not be supported would not be listed.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

#: format id -> what to call it when someone has to choose (only `custom` asks).
FORMATS: dict[str, str] = {
    "openai-responses": "OpenAI (Responses API)",
    "openai-chat": "OpenAI-compatible (Chat Completions)",
    "anthropic-messages": "Anthropic (Messages API)",
    "gemini-generatecontent": "Google Gemini (generateContent)",
}


@dataclass(frozen=True)
class Kind:
    id: str
    label: str
    #: `None` only for `custom`, where the person chooses.
    format: str | None
    #: The address used when none is given. `None` where one must be.
    base_url: str | None
    #: 'fixed' (never asked), 'editable' (a default they may change), 'required'.
    address: str
    #: 'required' | 'optional' | 'none' — whether a key is asked for.
    key: str
    blurb: str


KINDS: dict[str, Kind] = {
    k.id: k
    for k in (
        Kind("openai", "OpenAI", "openai-responses", "https://api.openai.com/v1",
             "fixed", "required", "GPT models, using your OpenAI API key."),
        Kind("anthropic", "Anthropic", "anthropic-messages", "https://api.anthropic.com/v1",
             "fixed", "required", "Claude models, using your Anthropic API key."),
        Kind("gemini", "Google Gemini", "gemini-generatecontent",
             "https://generativelanguage.googleapis.com/v1beta",
             "fixed", "required", "Gemini models, using your Google AI API key."),
        Kind("ollama", "Ollama", "openai-chat", "http://127.0.0.1:11434/v1",
             "editable", "none", "Models running on this computer through Ollama."),
        Kind("lmstudio", "LM Studio", "openai-chat", "http://127.0.0.1:1234/v1",
             "editable", "optional", "Models running on this computer through LM Studio."),
        Kind("custom", "Custom", None, None, "required", "optional",
             "Any other provider, at an address you give, in a format you choose."),
    )
}

#: Kinds where a bare `http://host:port` should be read as the server's `/v1`.
_V1_KINDS = {"ollama", "lmstudio"}


class InvalidAddress(ValueError):
    """A plain-language reason an address cannot be used."""


def normalize_base_url(kind_id: str, url: str | None) -> str | None:
    """The address to store, or `None` for 'use the kind's own'.

    Fixed kinds never store one — a stale or mistyped address on OpenAI's own
    connection is a bug nobody can see. Everything else is checked for being a
    real web address, trimmed of a trailing slash, and (for the two local
    servers) given the `/v1` they serve their compatible API under when the
    person typed only the host and port.
    """
    kind = KINDS[kind_id]
    text = (url or "").strip()
    if kind.address == "fixed":
        return None
    if not text:
        if kind.address == "required":
            raise InvalidAddress("Enter the address of the provider, like https://example.com/v1.")
        return None
    parsed = urlparse(text if "://" in text else f"http://{text}")
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise InvalidAddress("That doesn't look like a web address. It should start with http:// or https://.")
    path = parsed.path.rstrip("/")
    if kind_id in _V1_KINDS and not path:
        path = "/v1"
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def base_url_for(kind_id: str, stored: str | None) -> str:
    """The address a request goes to."""
    kind = KINDS[kind_id]
    return stored or kind.base_url or ""


def public_kinds() -> list[dict]:
    """The picker's contents — what each kind needs, nothing about how it works."""
    return [
        {
            "id": k.id,
            "label": k.label,
            "blurb": k.blurb,
            "address": k.address,
            "defaultAddress": k.base_url,
            "key": k.key,
            "chooseFormat": k.format is None,
        }
        for k in KINDS.values()
    ]


def public_formats() -> list[dict]:
    return [{"id": fid, "label": label} for fid, label in FORMATS.items()]
